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

import enum
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

MAX_CANONICAL_JSON_DEPTH = 128
MAX_CANONICAL_JSON_NODES = 100_000
MAX_CANONICAL_JSON_BYTES = 16_777_216
MAX_CANONICAL_JSON_STRING_BYTES = 16_777_216
MAX_CANONICAL_JSON_STRING_CHARS = MAX_CANONICAL_JSON_STRING_BYTES // 6
MAX_CANONICAL_JSON_INTEGER = (2**63) - 1


class CanonicalJsonRefusalCode(enum.StrEnum):
    """Stable reasons a value cannot enter a canonical JSON preimage."""

    float_forbidden = "float_forbidden"
    non_string_key = "non_string_key"
    unsupported_type = "unsupported_type"
    container_error = "container_error"
    cyclic_value = "cyclic_value"
    depth_exceeded = "depth_exceeded"
    node_limit_exceeded = "node_limit_exceeded"
    byte_limit_exceeded = "byte_limit_exceeded"
    integer_out_of_range = "integer_out_of_range"


class CanonicalJsonRefusal(ValueError):
    """Typed, value-safe refusal raised by :func:`canonical_json_bytes`."""

    def __init__(self, code: CanonicalJsonRefusalCode, *, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(f"canonical JSON refused: {code.value} at {path}")


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
    materialized = _materialize_canonical_json_value(
        value,
        path="$",
        depth=0,
        active=set(),
        nodes=[0],
        bytes_used=[0],
    )
    encoded = json.dumps(
        materialized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_CANONICAL_JSON_BYTES:
        raise CanonicalJsonRefusal(
            CanonicalJsonRefusalCode.byte_limit_exceeded,
            path="$",
        )
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
    if len(domain) <= 1 or not domain.endswith(b"\0") or domain.count(b"\0") != 1:
        raise ValueError(
            "hash domain must be non-empty with exactly one terminating NUL"
        )
    try:
        domain.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("hash domain must be ASCII") from exc
    return hashlib.sha256(domain + canonical_json_bytes(value)).hexdigest()


def _materialize_canonical_json_value(
    value: Any,
    *,
    path: str,
    depth: int,
    active: set[int],
    nodes: list[int],
    bytes_used: list[int],
) -> Any:
    nodes[0] += 1
    if nodes[0] > MAX_CANONICAL_JSON_NODES:
        raise CanonicalJsonRefusal(
            CanonicalJsonRefusalCode.node_limit_exceeded,
            path=path,
        )
    if depth > MAX_CANONICAL_JSON_DEPTH:
        raise CanonicalJsonRefusal(
            CanonicalJsonRefusalCode.depth_exceeded,
            path=path,
        )
    if value is None or isinstance(value, (bool, str)):
        _consume_scalar_bytes(value, path=path, bytes_used=bytes_used)
        return value
    if isinstance(value, int):
        if not -MAX_CANONICAL_JSON_INTEGER <= value <= MAX_CANONICAL_JSON_INTEGER:
            raise CanonicalJsonRefusal(
                CanonicalJsonRefusalCode.integer_out_of_range,
                path=path,
            )
        _consume_scalar_bytes(value, path=path, bytes_used=bytes_used)
        return value
    if isinstance(value, float):
        raise CanonicalJsonRefusal(
            CanonicalJsonRefusalCode.float_forbidden,
            path=path,
        )
    if isinstance(value, Mapping):
        return _materialize_mapping(
            value,
            path=path,
            depth=depth,
            active=active,
            nodes=nodes,
            bytes_used=bytes_used,
        )
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return _materialize_sequence(
            value,
            path=path,
            depth=depth,
            active=active,
            nodes=nodes,
            bytes_used=bytes_used,
        )
    raise CanonicalJsonRefusal(
        CanonicalJsonRefusalCode.unsupported_type,
        path=path,
    )


def _materialize_mapping(
    value: Mapping[Any, Any],
    *,
    path: str,
    depth: int,
    active: set[int],
    nodes: list[int],
    bytes_used: list[int],
) -> dict[str, Any]:
    identity = id(value)
    if identity in active:
        raise CanonicalJsonRefusal(
            CanonicalJsonRefusalCode.cyclic_value,
            path=path,
        )
    active.add(identity)
    result: dict[str, Any] = {}
    try:
        _consume_bytes(2, path=path, bytes_used=bytes_used)
        for index, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                raise CanonicalJsonRefusal(
                    CanonicalJsonRefusalCode.non_string_key,
                    path=f"{path}.key[{index}]",
                )
            if index:
                _consume_bytes(1, path=path, bytes_used=bytes_used)
            _consume_scalar_bytes(
                key,
                path=f"{path}.key[{index}]",
                bytes_used=bytes_used,
            )
            _consume_bytes(1, path=path, bytes_used=bytes_used)
            result[key] = _materialize_canonical_json_value(
                item,
                path=f"{path}.value[{index}]",
                depth=depth + 1,
                active=active,
                nodes=nodes,
                bytes_used=bytes_used,
            )
    except CanonicalJsonRefusal:
        raise
    except Exception as exc:
        raise CanonicalJsonRefusal(
            CanonicalJsonRefusalCode.container_error,
            path=path,
        ) from exc
    finally:
        active.remove(identity)
    return result


def _materialize_sequence(
    value: Sequence[Any],
    *,
    path: str,
    depth: int,
    active: set[int],
    nodes: list[int],
    bytes_used: list[int],
) -> list[Any]:
    identity = id(value)
    if identity in active:
        raise CanonicalJsonRefusal(
            CanonicalJsonRefusalCode.cyclic_value,
            path=path,
        )
    active.add(identity)
    result: list[Any] = []
    try:
        _consume_bytes(2, path=path, bytes_used=bytes_used)
        for index, item in enumerate(value):
            if index:
                _consume_bytes(1, path=path, bytes_used=bytes_used)
            result.append(
                _materialize_canonical_json_value(
                    item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    active=active,
                    nodes=nodes,
                    bytes_used=bytes_used,
                )
            )
    except CanonicalJsonRefusal:
        raise
    except Exception as exc:
        raise CanonicalJsonRefusal(
            CanonicalJsonRefusalCode.container_error,
            path=path,
        ) from exc
    finally:
        active.remove(identity)
    return result


def _consume_scalar_bytes(
    value: None | bool | int | str,
    *,
    path: str,
    bytes_used: list[int],
) -> None:
    if isinstance(value, str) and len(value) > MAX_CANONICAL_JSON_STRING_CHARS:
        raise CanonicalJsonRefusal(
            CanonicalJsonRefusalCode.byte_limit_exceeded,
            path=path,
        )
    encoded_size = len(
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    )
    if isinstance(value, str) and encoded_size - 2 > MAX_CANONICAL_JSON_STRING_BYTES:
        raise CanonicalJsonRefusal(
            CanonicalJsonRefusalCode.byte_limit_exceeded,
            path=path,
        )
    _consume_bytes(encoded_size, path=path, bytes_used=bytes_used)


def _consume_bytes(
    count: int,
    *,
    path: str,
    bytes_used: list[int],
) -> None:
    bytes_used[0] += count
    if bytes_used[0] > MAX_CANONICAL_JSON_BYTES:
        raise CanonicalJsonRefusal(
            CanonicalJsonRefusalCode.byte_limit_exceeded,
            path=path,
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
