"""``anvil.redaction.redact_secrets`` — what it removes, and what it must leave alone."""

from __future__ import annotations

import time

import pytest

from anvil.redaction import REDACTED, redact_secrets

_S = "s3cr3t-Value_123"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "pytest tests/ -q",
        "ruff check .",
        "mypy src",
        "npm test",
        "cargo test -- --nocapture",
        "bun test",
        'uv run --locked pytest -k "not slow"',
        "PYTHONPATH=src pytest -x",
        # A file or test named after a credential is not a credential.
        "pytest tests/test_token.py::test_refresh",
        "pytest tests/test_secrets.py -k password",
        "pytest --cov=anvil.secret_store",
        # Ports are not passwords; a comparison is not an assignment.
        "curl http://localhost:8000/path",
        "assert token == expected",
        "secrets:\n  enabled",
        "3 passed, 1 warning in 0.42s",
    ],
)
def test_text_without_a_secret_is_returned_unchanged(text: str) -> None:
    assert redact_secrets(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"API_KEY={_S} pytest", f"API_KEY={REDACTED} pytest"),
        (f"export GITHUB_TOKEN='{_S}' && pytest", f"export GITHUB_TOKEN={REDACTED} && pytest"),
        (f'DB_PASSWORD="{_S} two" mypy', f"DB_PASSWORD={REDACTED} mypy"),
        (f"db.password = {_S}", f"db.password = {REDACTED}"),
        (f"client_secret: {_S}", f"client_secret: {REDACTED}"),
        (f"X-Auth-Token: {_S}", f"X-Auth-Token: {REDACTED}"),
        (f'{{"password": "{_S}", "user": "bob"}}', f'{{"password": {REDACTED}, "user": "bob"}}'),
        (f"GET /x?a=1&access_token={_S}&b=2", f"GET /x?a=1&access_token={REDACTED}&b=2"),
        (f"AWS_CREDENTIAL={_S}", f"AWS_CREDENTIAL={REDACTED}"),
        (f"passwd={_S}", f"passwd={REDACTED}"),
        (
            f"pytest --token {_S} --password={_S} --api-key '{_S} x'",
            f"pytest --token {REDACTED} --password={REDACTED} --api-key {REDACTED}",
        ),
        (
            f"curl -H 'Authorization: Bearer {_S}' https://example.test",
            f"curl -H 'Authorization: Bearer {REDACTED}' https://example.test",
        ),
        (f"authorization: Basic {_S}", f"authorization: Basic {REDACTED}"),
        ("Bearer abcdefghijklmnopqrstuvwxyz", f"Bearer {REDACTED}"),
        (
            f"psql postgres://app:{_S}@db:5432/x",
            f"psql postgres://app:{REDACTED}@db:5432/x",
        ),
        (
            f"git clone https://x-access-token:{_S}@github.com/a/b",
            f"git clone https://x-access-token:{REDACTED}@github.com/a/b",
        ),
        ("key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA used", f"key {REDACTED} used"),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", REDACTED),
        ("github_pat_11ABCDEFG0abcdefghijkl_mnopqrstuv", REDACTED),
        ("xoxb-1234567890-abcdefghij", REDACTED),
        ("id AKIAABCDEFGHIJKLMNOP done", f"id {REDACTED} done"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            REDACTED,
        ),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\nafter",
            f"{REDACTED}\nafter",
        ),
        # A quoted value whose quote never closes is redacted to the end of
        # its line, not kept because the pattern wanted a closing quote.
        (f'PASSWORD="{_S}', f"PASSWORD={REDACTED}"),
        (f"export API_TOKEN='{_S} tail\nnext line", f"export API_TOKEN={REDACTED}\nnext line"),
        (f'pytest --password "{_S}', f"pytest --password {REDACTED}"),
        # A key block cut off before its END marker is redacted to the end.
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk", REDACTED),
    ],
)
def test_secret_values_are_replaced_and_context_is_kept(text: str, expected: str) -> None:
    redacted = redact_secrets(text)
    assert redacted == expected
    assert _S not in redacted
    # Redacting already-redacted text changes nothing.
    assert redact_secrets(redacted) == redacted


@pytest.mark.parametrize(
    "text",
    [
        "a" * 200_000,
        "-" * 200_000,
        "=" * 200_000,
        "a." * 100_000,
        "token." * 30_000,
        "token=" * 30_000,
        "token: " * 30_000,
        "--token " * 25_000,
        "a-token.x_" * 20_000,
        "eyJ" * 60_000,
        "sk-" * 60_000,
        "x://" * 50_000,
        "x://u:" + "p" * 200_000,
        "bearer " * 30_000,
        "authorization: bearer " * 10_000,
        "-----BEGIN PRIVATE KEY-----" * 8_000,
        'password="' + "a" * 200_000,
        "password=\n" * 30_000,
    ],
)
def test_adversarial_input_is_redacted_in_bounded_time(text: str) -> None:
    # Command output is attacker-influenced; no pattern may backtrack
    # super-linearly. Each of these runs in well under 0.2s; the generous
    # ceiling only guards against a quadratic regression on a slow runner.
    started = time.perf_counter()
    redact_secrets(text)
    assert time.perf_counter() - started < 5.0
