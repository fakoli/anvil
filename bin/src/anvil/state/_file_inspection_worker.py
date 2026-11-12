"""Closed finite worker protocol; raw DB descriptors never enter the owner."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path


def _database_files(database: Path) -> dict[str, bytes]:
    digests = {}
    for suffix in ("", "-wal", "-journal"):
        path = Path(f"{database}{suffix}")
        if path.exists():
            with path.open("rb") as source:
                digests[suffix] = hashlib.file_digest(source, "sha256").digest()
    return digests


def _copy_database(source: Path, destination: Path) -> None:
    """Stream a byte-stable private DB/WAL/journal copy without opening SQLite."""
    if source.resolve() == destination.resolve():
        raise ValueError("snapshot destination must differ from source")
    for _ in range(3):
        for suffix in ("", "-wal", "-journal"):
            Path(f"{destination}{suffix}").unlink(missing_ok=True)
        try:
            for suffix in ("", "-wal", "-journal"):
                current = Path(f"{source}{suffix}")
                if current.exists():
                    target = Path(f"{destination}{suffix}")
                    shutil.copyfile(current, target)
                    target.chmod(0o600)
            captured = _database_files(destination)
            if "" not in captured:
                raise ValueError("snapshot database is unavailable")
            if captured == _database_files(source):
                return
        except FileNotFoundError:
            # A concurrent last-close checkpoint can remove a sidecar. Recopy
            # the entire observed set; never keep stale sidecars from a retry.
            continue
    raise ValueError("database changed during private snapshot")


def _inspect(request: dict[str, object]) -> dict[str, object]:
    from anvil.state.backend import SchemaMismatch

    try:
        if set(request) == {"path"} and isinstance(request["path"], str):
            from anvil.state.sqlite import _read_db_schema_version_in_worker

            return {"ok": True, "version": _read_db_schema_version_in_worker(request["path"])}
        operation = request.get("operation")
        if operation in {"copy_database", "copy_tree"}:
            if set(request) != {"operation", "source", "destination"} or not all(
                isinstance(request[key], str) for key in ("source", "destination")
            ):
                raise ValueError
            source, destination = Path(request["source"]), Path(request["destination"])
            if operation == "copy_database":
                _copy_database(source, destination)
            else:
                shutil.copytree(source, destination)
        elif operation == "fsync":
            paths = request.get("paths")
            if set(request) != {"operation", "paths"} or not isinstance(paths, list) or not (
                1 <= len(paths) <= 64 and all(isinstance(path, str) for path in paths)
            ):
                raise ValueError
            for path in paths:
                with Path(path).open("r+b") as stream:
                    os.fsync(stream.fileno())
        else:
            raise ValueError
        return {"ok": True}
    except SchemaMismatch as exc:
        if getattr(exc, "code", None) == "schema_mismatch":
            return {
                "ok": False, "code": "schema_mismatch", "actual": exc.actual,
                "expected": exc.expected, "direction": exc.direction,
            }
    except Exception:  # noqa: BLE001 - closed protocol never emits exception material
        pass
    return {"ok": False, "code": "schema_probe_failed"}


def main() -> int:
    for request_line in sys.stdin:
        try:
            request = json.loads(request_line)
            if not isinstance(request, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            payload = {"ok": False, "code": "schema_probe_failed"}
        else:
            payload = _inspect(request)
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
