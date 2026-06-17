"""Hash-chained event ids for git-backed event logs.

Phase A of the git-backed-events spec (docs/specs/2026-06-10-git-backed-events.md):
in ``events_storage: git`` mode, event ids are content hashes chained through
the previous event's id instead of machine-local sequence numbers, so two
branches/machines can append concurrently and merge later with zero collision
risk.

Pure functions only — no I/O, no clock, no SQLite. Shared by the backend write
path (``state/sqlite.py``), the migration command (``cli/migrate.py``), and the
tests, so the three can never drift on what "the" hash of an event is.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# 12 hex chars = 48 bits. The birthday bound puts a 50% collision chance at
# ~2^24 ≈ 16.7M events — far beyond any project log — and the parent-id chain
# plus event identity means two distinct same-parent events cannot collide just
# because they share a payload, actor, and timestamp.
_HASH_HEX_LEN = 12

EVENT_HASH_ID_PREFIX = "E-"


def canonical_payload_json(payload: dict[str, Any]) -> str:
    """Serialize *payload* to canonical JSON for hashing.

    Sorted keys + compact separators so that semantically identical payloads
    hash identically regardless of dict insertion order. ``ensure_ascii``
    stays at the json-module default (True) so the hashed byte form is
    ASCII-stable across platforms and locales.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def hash_event_id(
    *,
    parent_event_id: str | None,
    action: str,
    target_kind: str,
    target_id: str,
    payload: dict[str, Any],
    actor: str,
    ts: str,
) -> str:
    """Return the hash-chained event id per the git-backed-events spec.

    The sha256 input is the following fields joined with the ASCII unit
    separator ``"\\x1f"``::

        parent_event_id, action, target_kind, target_id,
        canonical_json(payload), actor, ts

    The id is the first 12 hex chars of that digest, prefixed with ``"E-"``.

    ``parent_event_id`` is ``None`` for the first event in a log and then
    contributes an empty string to the hash input. ``ts`` is the event
    timestamp as an ISO 8601 string — callers pass
    ``timestamp.isoformat()`` so the writer and the migration command hash
    the exact same material.
    """
    material = "\x1f".join((
        parent_event_id or "",
        action,
        target_kind,
        target_id,
        canonical_payload_json(payload),
        actor,
        ts,
    ))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return EVENT_HASH_ID_PREFIX + digest[:_HASH_HEX_LEN]
