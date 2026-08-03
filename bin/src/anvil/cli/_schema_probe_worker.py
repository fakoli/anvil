"""One-shot, closed-output worker for cancellable schema inspection."""

from __future__ import annotations

import json
import sys


def _inspect(path: str) -> dict[str, object]:
    """Inspect one path and return only the closed parent/worker protocol."""
    from anvil.state.backend import SchemaMismatch
    from anvil.state.sqlite import read_db_schema_version

    try:
        version = read_db_schema_version(path)
    except SchemaMismatch as exc:
        payload = {
            "ok": False,
            "code": getattr(exc, "code", "schema_probe_failed"),
            "actual": exc.actual,
            "expected": exc.expected,
            "direction": exc.direction,
        }
    except Exception:  # noqa: BLE001 - never expose worker exception material
        return {"ok": False, "code": "schema_probe_failed"}
    else:
        return {"ok": True, "version": version}
    return payload


def main() -> int:
    """Serve probes until the parent closes stdin; flush exactly one line each."""
    for request_line in sys.stdin:
        try:
            request = json.loads(request_line)
            path = request["path"]
            if not isinstance(path, str) or set(request) != {"path"}:
                raise ValueError
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            payload: dict[str, object] = {
                "ok": False,
                "code": "schema_probe_failed",
            }
        else:
            payload = _inspect(path)
        sys.stdout.write(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
