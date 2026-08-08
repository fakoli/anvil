"""Fail-closed, side-effect-free access to persisted PRD source bytes.

Section selectors are case-sensitive slash-delimited ATX heading paths.  Path
segments use JSON Pointer escaping: ``~0`` denotes ``~`` and ``~1`` denotes
``/``; every other tilde escape is invalid.  The document title (level 1) is
not part of a path; ``Goals/Primary`` selects the
``### Primary`` subsection beneath ``## Goals``.  Headings inside fenced code
blocks are inert.  Duplicate or overlapping selections are refused, and
selected sections are returned in source order without inserted bytes.

The v1 content digest is SHA-256 over::

    b"anvil.prd-content.v1\0" + source_digest_ascii + b"\0" +
    canonical_json(selector) + b"\0" + returned_source_bytes

The explicit full-document selector is ``{"kind":"full","paths":[]}``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from anvil.read_contracts import (
    PRD_CONTENT_OPERATION_ID,
    PRD_CONTENT_OPERATION_VERSION,
    PRD_CONTENT_SCHEMA_ID,
    PROVIDER_LIMITS_V1,
    PrdScopedRefV1,
    ReadErrorCode,
    ReadErrorV1,
)
from anvil.state.hashing import canonical_json_bytes
from anvil.state.schema import SCHEMA_VERSION

PRD_CONTENT_DIGEST_DOMAIN = b"anvil.prd-content.v1\0"
MAX_SECTION_SELECTORS = 128
MAX_SECTION_SELECTOR_BYTES = 4096
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEGMENT_RE = re.compile(r"^(?:[^/~\x00-\x1f\x7f]|~[01])+$")
_ATX_RE = re.compile(rb"^[ ]{0,3}(#{1,6})(?:[ \t]+|$)(.*?)(?:\r?\n)?$")
_FENCE_RE = re.compile(rb"^[ ]{0,3}(`{3,}|~{3,})(.*?)(?:\r?\n)?$")


class PrdContentResponseV1(BaseModel):
    """Closed successful wire document for ``state.prd.content`` v1."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation_id: Literal["state.prd.content"] = PRD_CONTENT_OPERATION_ID
    operation_version: Literal[1] = PRD_CONTENT_OPERATION_VERSION
    schema_id: Literal["anvil.state.prd-content.v1"] = PRD_CONTENT_SCHEMA_ID
    prd_ref: PrdScopedRefV1
    prd_revision: int = Field(ge=1)
    digest_algorithm: Literal["sha256"] = "sha256"
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_size_bytes: int = Field(ge=0)
    returned_size_bytes: int = Field(ge=0)
    encoding: Literal["utf-8"] = "utf-8"
    selected_sections: list[str] = Field(max_length=MAX_SECTION_SELECTORS)
    applied_limit_bytes: int = Field(ge=1)
    truncated: Literal[False] = False
    content: str


class PrdContentRefusal(Exception):
    """A closed refusal that contains no source bytes or exception text."""

    def __init__(self, error: ReadErrorV1) -> None:
        self.error = error
        super().__init__(error.code.value)


@dataclass(frozen=True)
class _Heading:
    path: str
    level: int
    start: int
    end: int


def _refuse(
    code: ReadErrorCode,
    *,
    field: str | None = None,
    actual: int | None = None,
    limit: int | None = None,
) -> PrdContentRefusal:
    return PrdContentRefusal(
        ReadErrorV1(code=code, field=field, actual=actual, limit=limit)
    )


def _validated_request(
    prd_id: str,
    *,
    sections: list[str] | None,
    expected_digest: str | None,
    max_bytes: int | None,
) -> tuple[PrdScopedRefV1, list[str], str | None, int]:
    try:
        ref = PrdScopedRefV1(prd_id=prd_id)
    except (TypeError, ValueError) as exc:
        raise _refuse(ReadErrorCode.invalid_identifier, field="prd_id") from exc

    if expected_digest is not None and not _SHA256_RE.fullmatch(expected_digest):
        raise _refuse(ReadErrorCode.invalid_request, field="expected_digest")

    ceiling = PROVIDER_LIMITS_V1.max_prd_content_bytes
    if max_bytes is None:
        applied_limit = ceiling
    elif type(max_bytes) is not int or not 1 <= max_bytes <= ceiling:
        raise _refuse(
            ReadErrorCode.invalid_request,
            field="request",
            actual=max_bytes if type(max_bytes) is int and max_bytes >= 0 else None,
            limit=ceiling,
        )
    else:
        applied_limit = max_bytes

    selected = list(sections or [])
    if len(selected) > MAX_SECTION_SELECTORS:
        raise _refuse(
            ReadErrorCode.limit_exceeded,
            field="sections",
            actual=len(selected),
            limit=MAX_SECTION_SELECTORS,
        )
    seen: set[str] = set()
    for selector in selected:
        if type(selector) is not str:
            raise _refuse(ReadErrorCode.invalid_section, field="sections")
        try:
            encoded = selector.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _refuse(ReadErrorCode.invalid_section, field="sections") from exc
        segments = selector.split("/")
        if (
            not selector
            or len(encoded) > MAX_SECTION_SELECTOR_BYTES
            or any(
                not segment
                or segment != segment.strip()
                or segment in {".", ".."}
                or _SEGMENT_RE.fullmatch(segment) is None
                or any(
                    unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                    for character in segment
                )
                for segment in segments
            )
            or selector in seen
        ):
            raise _refuse(ReadErrorCode.invalid_section, field="sections")
        seen.add(selector)
    return ref, selected, expected_digest, applied_limit


def validate_prd_content_request(
    prd_id: str,
    *,
    sections: list[str] | None = None,
    expected_digest: str | None = None,
    max_bytes: int | None = None,
) -> None:
    """Validate every caller-controlled field without opening project state."""
    _validated_request(
        prd_id,
        sections=sections,
        expected_digest=expected_digest,
        max_bytes=max_bytes,
    )


def parse_prd_content_limit(value: str | None) -> int | None:
    """Parse the CLI limit inside the command's closed JSON boundary."""
    if value is None:
        return None
    if not value or len(value) > 10 or not value.isascii() or not value.isdecimal():
        raise _refuse(ReadErrorCode.invalid_request, field="request")
    parsed = int(value)
    if not 1 <= parsed <= PROVIDER_LIMITS_V1.max_prd_content_bytes:
        raise _refuse(
            ReadErrorCode.invalid_request,
            field="request",
            actual=parsed,
            limit=PROVIDER_LIMITS_V1.max_prd_content_bytes,
        )
    return parsed


def _event_material_digest(
    action: object,
    target_kind: object,
    target_id: object,
    payload: object,
) -> bytes:
    try:
        material = canonical_json_bytes(
            {
                "action": action,
                "target_kind": target_kind,
                "target_id": target_id,
                "payload_json": payload,
            }
        )
    except (TypeError, ValueError) as exc:
        raise _refuse(ReadErrorCode.projection_not_converged, field="projection") from exc
    return hashlib.sha256(material).digest()


def _log_event_material(events_path: Path) -> dict[str, bytes]:
    try:
        signature_before = events_path.stat()
        events: dict[str, bytes] = {}
        with events_path.open("rb") as stream:
            lines = stream.readlines()
        for index, raw_line in enumerate(lines):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if index == len(lines) - 1 and not raw_line.endswith(b"\n"):
                    continue
                raise _refuse(ReadErrorCode.projection_not_converged, field="projection") from exc
            event_id = value.get("id") if isinstance(value, dict) else None
            if not isinstance(event_id, str) or not event_id:
                raise _refuse(ReadErrorCode.projection_not_converged, field="projection")
            material = _event_material_digest(
                value.get("action"),
                value.get("target_kind"),
                value.get("target_id"),
                value.get("payload_json"),
            )
            prior = events.setdefault(event_id, material)
            if prior != material:
                raise _refuse(ReadErrorCode.projection_not_converged, field="projection")
        signature_after = events_path.stat()
    except PrdContentRefusal:
        raise
    except OSError as exc:
        raise _refuse(ReadErrorCode.state_unavailable, field="state") from exc
    if (
        signature_before.st_size != signature_after.st_size
        or signature_before.st_mtime_ns != signature_after.st_mtime_ns
    ):
        raise _refuse(ReadErrorCode.projection_not_converged, field="projection")
    return events


def _table_event_material(connection: sqlite3.Connection) -> dict[str, bytes]:
    events: dict[str, bytes] = {}
    try:
        rows = connection.execute(
            "SELECT id, action, target_kind, target_id, payload_json FROM events"
        ).fetchall()
        for row in rows:
            payload = json.loads(row[4])
            events[row[0]] = _event_material_digest(row[1], row[2], row[3], payload)
    except PrdContentRefusal:
        raise
    except (sqlite3.Error, TypeError, json.JSONDecodeError) as exc:
        raise _refuse(ReadErrorCode.projection_not_converged, field="projection") from exc
    return events


def _open_readonly(state_dir: Path) -> sqlite3.Connection:
    db_path = state_dir / "state.db"
    if not state_dir.is_dir() or not db_path.is_file():
        raise _refuse(ReadErrorCode.state_unavailable, field="state")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if type(version) is not int or version != SCHEMA_VERSION:
            connection.close()
            raise _refuse(ReadErrorCode.schema_incompatible, field="schema")
        connection.execute("BEGIN")
        return connection
    except PrdContentRefusal:
        raise
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise _refuse(ReadErrorCode.state_unavailable, field="state") from exc


def _validated_source(row: sqlite3.Row) -> tuple[bytes, str, int]:
    revision = row["revision"]
    source = row["source_bytes"]
    digest = row["source_sha256"]
    size = row["source_size_bytes"]
    if (
        type(revision) is not int
        or revision < 1
        or row["provenance_state"] != "available"
        or row["content_available"] != 1
        or source is None
        or digest is None
        or size is None
        or row["source_encoding"] != "utf-8"
        or row["source_revision"] != revision
    ):
        raise _refuse(ReadErrorCode.content_unavailable, field="content")
    try:
        source_bytes = bytes(source)
    except (TypeError, ValueError) as exc:
        raise _refuse(ReadErrorCode.source_drift, field="content") from exc
    if (
        type(size) is not int
        or size != len(source_bytes)
        or size > PROVIDER_LIMITS_V1.max_prd_content_bytes
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
        or hashlib.sha256(source_bytes).hexdigest() != digest
    ):
        raise _refuse(ReadErrorCode.source_drift, field="content")
    return source_bytes, digest, revision


def _source_binding_is_projected(
    connection: sqlite3.Connection,
    ref: PrdScopedRefV1,
    source: bytes,
    source_digest: str,
    revision: int,
) -> bool:
    try:
        rows = connection.execute(
            "SELECT action, payload_json FROM events "
            "WHERE action IN ('prd.parsed', 'prd.revised')"
        ).fetchall()
        for action, payload_json in rows:
            payload = json.loads(payload_json)
            if not isinstance(payload, dict):
                continue
            event_prd_id = payload.get("prd_id", "default")
            event_revision = (
                1 if action == "prd.parsed" else payload.get("revision")
            )
            if event_prd_id != ref.prd_id or event_revision != revision:
                continue
            source_text = payload.get("source_text")
            if not isinstance(source_text, str):
                continue
            try:
                event_source = source_text.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                continue
            if (
                event_source == source
                and payload.get("source_sha256") == source_digest
                and payload.get("source_size_bytes") == len(source)
                and payload.get("source_encoding") == "utf-8"
                and payload.get("source_revision") == revision
                and payload.get("provenance_state") == "available"
                and payload.get("content_available") is True
            ):
                return True
    except (sqlite3.Error, TypeError, json.JSONDecodeError) as exc:
        raise _refuse(ReadErrorCode.projection_not_converged, field="projection") from exc
    return False


def _heading_text(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = re.sub(r"[ \t]+#+[ \t]*$", "", text).strip(" \t")
    return text or None


def _selector_segment(title: str) -> str:
    return title.replace("~", "~0").replace("/", "~1")


def _section_index(source: bytes) -> dict[str, list[_Heading]]:
    lines = source.splitlines(keepends=True)
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)

    raw_headings: list[tuple[str, int, int]] = []
    stack: list[tuple[int, str]] = []
    fence_char: bytes | None = None
    fence_size = 0
    for offset, line in zip(offsets, lines, strict=True):
        fence = _FENCE_RE.match(line)
        if fence is not None:
            marker = fence.group(1)
            tail = fence.group(2).strip()
            if fence_char is None:
                fence_char, fence_size = marker[:1], len(marker)
                continue
            if marker[:1] == fence_char and len(marker) >= fence_size and not tail:
                fence_char, fence_size = None, 0
                continue
        if fence_char is not None:
            continue
        match = _ATX_RE.match(line)
        if match is None:
            continue
        level = len(match.group(1))
        title = _heading_text(match.group(2))
        if title is None:
            continue
        if level == 1:
            stack.clear()
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, _selector_segment(title)))
        raw_headings.append(("/".join(part for _, part in stack), level, offset))

    index: dict[str, list[_Heading]] = {}
    for item_index, (path, level, start) in enumerate(raw_headings):
        end = len(source)
        for _, following_level, following_start in raw_headings[item_index + 1 :]:
            if following_level <= level:
                end = following_start
                break
        index.setdefault(path, []).append(_Heading(path, level, start, end))
    return index


def _select(source: bytes, requested: list[str]) -> tuple[bytes, list[str], dict[str, Any]]:
    if not requested:
        return source, [], {"kind": "full", "paths": []}
    index = _section_index(source)
    headings: list[_Heading] = []
    for selector in requested:
        matches = index.get(selector, [])
        if len(matches) != 1:
            raise _refuse(ReadErrorCode.invalid_section, field="sections")
        headings.append(matches[0])
    headings.sort(key=lambda heading: heading.start)
    if any(left.end > right.start for left, right in zip(headings, headings[1:], strict=False)):
        raise _refuse(ReadErrorCode.invalid_section, field="sections")
    ordered = [heading.path for heading in headings]
    returned = b"".join(source[heading.start : heading.end] for heading in headings)
    return returned, ordered, {"kind": "sections", "paths": ordered}


def _content_digest(source_digest: str, selector: dict[str, Any], content: bytes) -> str:
    preimage = (
        PRD_CONTENT_DIGEST_DOMAIN
        + source_digest.encode("ascii")
        + b"\0"
        + canonical_json_bytes(selector)
        + b"\0"
        + content
    )
    return hashlib.sha256(preimage).hexdigest()


def read_prd_content(
    state_dir: Path,
    prd_id: str,
    *,
    sections: list[str] | None = None,
    expected_digest: str | None = None,
    max_bytes: int | None = None,
) -> PrdContentResponseV1:
    """Read one persisted PRD source through the closed v1 contract."""
    ref, requested, expected, applied_limit = _validated_request(
        prd_id,
        sections=sections,
        expected_digest=expected_digest,
        max_bytes=max_bytes,
    )
    connection = _open_readonly(state_dir)
    try:
        try:
            table_events = _table_event_material(connection)
            if table_events != _log_event_material(state_dir / "events.jsonl"):
                raise _refuse(
                    ReadErrorCode.projection_not_converged, field="projection"
                )
            row = connection.execute(
                "SELECT id, revision, source_bytes, source_sha256, "
                "source_size_bytes, source_encoding, source_revision, "
                "provenance_state, content_available FROM prds WHERE id = ?",
                (ref.prd_id,),
            ).fetchone()
        except PrdContentRefusal:
            raise
        except sqlite3.Error as exc:
            raise _refuse(ReadErrorCode.state_unavailable, field="state") from exc
        if row is None:
            raise _refuse(ReadErrorCode.prd_not_found, field="prd_id")
        source, source_digest, revision = _validated_source(row)
        try:
            source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _refuse(ReadErrorCode.invalid_utf8, field="content") from exc
        if not _source_binding_is_projected(
            connection, ref, source, source_digest, revision
        ):
            raise _refuse(
                ReadErrorCode.projection_not_converged, field="projection"
            )
        if expected is not None and expected != source_digest:
            raise _refuse(ReadErrorCode.stale_digest, field="expected_digest")
        returned, ordered, canonical_selector = _select(source, requested)
        if len(returned) > applied_limit:
            raise _refuse(
                ReadErrorCode.limit_exceeded,
                field="content",
                actual=len(returned),
                limit=applied_limit,
            )
        try:
            content = returned.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _refuse(ReadErrorCode.invalid_utf8, field="content") from exc
        return PrdContentResponseV1(
            prd_ref=ref,
            prd_revision=revision,
            source_digest=source_digest,
            content_digest=_content_digest(
                source_digest, canonical_selector, returned
            ),
            source_size_bytes=len(source),
            returned_size_bytes=len(returned),
            selected_sections=ordered,
            applied_limit_bytes=applied_limit,
            content=content,
        )
    finally:
        connection.close()


__all__ = [
    "MAX_SECTION_SELECTORS",
    "PRD_CONTENT_DIGEST_DOMAIN",
    "PrdContentRefusal",
    "PrdContentResponseV1",
    "parse_prd_content_limit",
    "read_prd_content",
    "validate_prd_content_request",
]
