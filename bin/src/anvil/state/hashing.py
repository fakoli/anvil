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
from collections.abc import Mapping, Sequence
from typing import Any

# 12 hex chars = 48 bits. The birthday bound puts a 50% collision chance at
# ~2^24 ≈ 16.7M events — far beyond any project log — and the parent-id chain
# plus event identity means two distinct same-parent events cannot collide just
# because they share a payload, actor, and timestamp.
_HASH_HEX_LEN = 12

EVENT_HASH_ID_PREFIX = "E-"


def canonical_json_bytes(value: Any) -> bytes:
    """Return the portable canonical JSON representation of *value*.

    This is the public-contract canonicalizer.  It deliberately differs from
    :func:`canonical_payload_json`, whose historical ASCII representation is
    part of the git-event id contract.  Public read payloads are encoded as
    UTF-8, preserve every Unicode code point and newline byte represented by
    the input string, and reject floats instead of relying on implementation-
    specific or non-standard JSON spellings.

    The accepted value space is the JSON data model with integers but without
    floats: ``None``, booleans, integers, strings, mappings with string keys,
    and sequences other than text/bytes.  Refusing unsupported values keeps
    hashing independent of ``default=`` coercions and object ``repr`` output.
    """
    _validate_canonical_json_value(value, path="$")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    # ``json.dumps`` never emits these, but keep the byte-level invariants
    # executable at the boundary where future serializer changes would land.
    if encoded.startswith(b"\xef\xbb\xbf") or encoded.endswith((b"\n", b"\r")):
        raise ValueError("canonical JSON must not contain a BOM or trailing newline")
    return encoded


def domain_separated_sha256(domain: bytes, value: Any) -> str:
    """Hash canonical JSON under a NUL-terminated ASCII *domain*.

    Domain separation is part of the digest contract, so callers must provide
    a non-empty ASCII label ending in exactly one NUL byte.
    """
    if not domain or not domain.endswith(b"\0") or domain.endswith(b"\0\0"):
        raise ValueError("hash domain must be non-empty and NUL-terminated")
    try:
        domain.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("hash domain must be ASCII") from exc
    return hashlib.sha256(domain + canonical_json_bytes(value)).hexdigest()


def _validate_canonical_json_value(value: Any, *, path: str) -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise TypeError(f"floats are not allowed in canonical JSON at {path}")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"canonical JSON object key at {path} is not a string")
            _validate_canonical_json_value(item, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        for index, item in enumerate(value):
            _validate_canonical_json_value(item, path=f"{path}[{index}]")
        return
    raise TypeError(
        f"unsupported canonical JSON value {type(value).__name__} at {path}"
    )


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
