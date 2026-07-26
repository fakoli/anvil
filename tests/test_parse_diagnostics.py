"""Public PRD parser diagnostics remain useful, inert, and finite."""

from anvil.planning.diagnostics import (
    MAX_PARSE_ERROR_MESSAGE_UTF8_BYTES,
    parse_diagnostic_report,
)
from anvil.planning.template import ParseError


def test_small_safe_error_is_byte_compatible() -> None:
    message = "ordinary parser detail " * 20
    error = ParseError("features", 42, message)

    report = parse_diagnostic_report([error])

    assert report.entries == [error]
    assert report.total_count == report.shown_count == 1
    assert report.omitted_count == 0
    assert report.errors_truncated is False
    assert report.messages_truncated == 0
    assert report.sanitized_count == 0


def test_five_thousand_errors_are_capped_with_typed_counts() -> None:
    errors = [ParseError("features", index, f"bad heading {index}") for index in range(5_000)]

    report = parse_diagnostic_report(errors)

    assert len(report.entries) == 20
    assert report.total_count == 5_000
    assert report.shown_count == 20
    assert report.omitted_count == 4_980
    assert report.errors_truncated is True


def test_controls_are_escaped_and_multibyte_truncation_is_utf8_safe() -> None:
    raw = "\x1b]8;;https://evil.invalid\x07" + ("界" * 1_000)

    report = parse_diagnostic_report([ParseError("bad\nsection", -7, raw)])

    entry = report.entries[0]
    assert entry.section == "bad\\x0asection"
    assert entry.line == 0
    assert "\x1b" not in entry.message
    assert "\x07" not in entry.message
    assert "\\x1b" in entry.message
    assert "\\x07" in entry.message
    assert entry.message.endswith("... [message truncated]")
    assert len(entry.message.encode("utf-8")) <= MAX_PARSE_ERROR_MESSAGE_UTF8_BYTES
    assert report.messages_truncated == 1
    assert report.sanitized_count == 1
