"""Bounded, terminal-safe rendering for PRD parser diagnostics.

The parser deliberately collects every structural error so programmatic callers
can inspect a complete in-process result.  Public CLI and MCP boundaries must not
echo that unbounded, author-controlled collection: malformed documents can
produce thousands of headings and individual messages may contain heading text.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from anvil.planning.template import ParseError

MAX_PUBLIC_PARSE_ERRORS = 20
MAX_PARSE_ERROR_SECTION_UTF8_BYTES = 96
MAX_PARSE_ERROR_MESSAGE_UTF8_BYTES = 1024
MAX_PUBLIC_IDENTIFIER_LIST_UTF8_BYTES = 1024

_ALLOWED_FORMAT_CONTROLS = {"\u200c", "\u200d"}


def _escape_unsafe_codepoints(text: str) -> str:
    rendered: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        unsafe = category in {"Cc", "Cs", "Zl", "Zp"} or (
            category == "Cf" and char not in _ALLOWED_FORMAT_CONTROLS
        )
        if not unsafe:
            rendered.append(char)
            continue
        codepoint = ord(char)
        if codepoint <= 0xFF:
            rendered.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(f"\\U{codepoint:08x}")
    return "".join(rendered)


def _truncate_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8", errors="strict")
    if len(encoded) <= limit:
        return text
    suffix = "... [message truncated]"
    budget = limit - len(suffix)
    truncated = encoded[:budget]
    while True:
        try:
            return truncated.decode("utf-8") + suffix
        except UnicodeDecodeError:
            truncated = truncated[:-1]


def sanitize_parse_error_text(text: str, *, byte_limit: int) -> str:
    """Return bounded inert text without preserving terminal-active controls."""
    try:
        safe = _escape_unsafe_codepoints(text)
    except (TypeError, UnicodeError):
        return "<invalid diagnostic text>"
    return _truncate_utf8(safe, byte_limit)


def format_identifier_summary(identifiers: Iterable[str]) -> str:
    """Render a deterministic bounded identifier list for public refusals.

    Ordinary short, inert IDs stay actionable. Oversized or terminal-active
    collections become a stable aggregate fingerprint, so an author-controlled
    identifier cannot amplify a CLI/MCP diagnostic or inject another line.
    """
    values = sorted(identifiers)
    joined = ", ".join(values)
    try:
        safe = _escape_unsafe_codepoints(joined)
        raw = joined.encode("utf-8", errors="strict")
    except (TypeError, UnicodeError):
        safe = ""
        raw = joined.encode("utf-8", errors="replace")
    if safe == joined and len(raw) <= MAX_PUBLIC_IDENTIFIER_LIST_UTF8_BYTES:
        return joined
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return (
        f"<redacted count={len(values)} utf8_bytes={len(raw)} "
        f"sha256={digest}>"
    )


@dataclass(frozen=True)
class ParseDiagnosticReport:
    """Finite public projection of a complete in-process parser result."""

    entries: list[ParseError]
    total_count: int
    shown_count: int
    omitted_count: int
    errors_truncated: bool
    messages_truncated: int
    sanitized_count: int


def parse_diagnostic_report(errors: Iterable[ParseError]) -> ParseDiagnosticReport:
    """Return sanitized entries and explicit truncation metadata."""
    materialized = list(errors)
    visible = materialized[:MAX_PUBLIC_PARSE_ERRORS]
    result: list[ParseError] = []
    messages_truncated = 0
    sanitized_count = 0
    for error in visible:
        section = sanitize_parse_error_text(
            error.section,
            byte_limit=MAX_PARSE_ERROR_SECTION_UTF8_BYTES,
        )
        message = sanitize_parse_error_text(
            error.message,
            byte_limit=MAX_PARSE_ERROR_MESSAGE_UTF8_BYTES,
        )
        if len(_escape_unsafe_codepoints(error.message).encode("utf-8")) > (
            MAX_PARSE_ERROR_MESSAGE_UTF8_BYTES
        ):
            messages_truncated += 1
        if section != error.section or message != error.message:
            sanitized_count += 1
        result.append(
            ParseError(
                section=section,
                line=max(0, int(error.line)),
                message=message,
            )
        )
    omitted = len(materialized) - len(visible)
    return ParseDiagnosticReport(
        entries=result,
        total_count=len(materialized),
        shown_count=len(result),
        omitted_count=omitted,
        errors_truncated=bool(omitted),
        messages_truncated=messages_truncated,
        sanitized_count=sanitized_count,
    )


def bounded_parse_errors(errors: Iterable[ParseError]) -> list[ParseError]:
    """Compatibility convenience returning only the report's real entries."""
    return parse_diagnostic_report(errors).entries


def format_parse_error(error: ParseError) -> str:
    """Format one already-bounded diagnostic for human or exception output."""
    return f"[{error.section}:{error.line}] {error.message}"


def format_parse_error_summary(errors: Iterable[ParseError]) -> str:
    """Return a bounded one-line summary for error envelopes and ToolError."""
    report = parse_diagnostic_report(errors)
    summary = "; ".join(format_parse_error(error) for error in report.entries)
    if report.omitted_count:
        summary += (
            f"; showing {report.shown_count} of {report.total_count}; "
            f"{report.omitted_count} omitted"
        )
    return summary
