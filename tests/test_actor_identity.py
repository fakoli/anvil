from __future__ import annotations

import shlex

import pytest

from anvil.actors import (
    ACTOR_AUTH_NOTICE,
    ActorIdentityError,
    actor_continuation,
    actor_identity_context,
    canonicalize_new_actor,
    continuation_context,
    current_shell_kind,
    quote_actor_posix,
    quote_actor_powershell,
    render_actor_continuation,
    resolve_actor_input,
    safe_actor_display,
    safe_actor_for_human,
)
from anvil.cli import _actor_output


def test_new_actor_is_nfc_normalized_without_narrowing_safe_text() -> None:
    assert (
        canonicalize_new_actor("Cafe\u0301 worker $;& ' 🚀") == "Café worker $;& ' 🚀"
    )


def test_new_actor_utf8_limit_is_applied_after_normalization() -> None:
    assert canonicalize_new_actor("é" * 64) == "é" * 64
    with pytest.raises(ActorIdentityError, match="128 UTF-8 bytes"):
        canonicalize_new_actor("é" * 65)


@pytest.mark.parametrize(
    "actor",
    [
        "",
        "   ",
        "bad\x00actor",
        "bad\x1factor",
        "bad\x7factor",
        "bad\x80actor",
        "bad\x9factor",
        "bad\u2028actor",
        "bad\u2029actor",
        "bad\u061cactor",
        "bad\u200eactor",
        "bad\u202eactor",
        "bad\u2066actor",
        "bad\u2069actor",
        "bad\ud800actor",
    ],
)
def test_new_actor_rejects_unsafe_text(actor: str) -> None:
    with pytest.raises(ActorIdentityError):
        canonicalize_new_actor(actor)


def test_exact_resolution_distinguishes_absent_from_legacy_values() -> None:
    env = {"ANVIL_ACTOR": "  legacy\u0301  ", "ANVIL_GATE_ACTOR": "fallback"}
    assert resolve_actor_input(None, env) == "  legacy\u0301  "
    assert resolve_actor_input("", env) == ""
    assert resolve_actor_input(None, {}) is None


@pytest.mark.parametrize(
    "environment",
    [
        {"MSYSTEM": "MINGW64"},
        {"CYGWIN": "winsymlinks:native"},
        {"OSTYPE": "msys"},
        {"SHELL": "C:/Program Files/Git/usr/bin/bash.exe"},
    ],
)
def test_windows_posix_shells_are_not_misidentified_as_powershell(
    environment: dict[str, str],
) -> None:
    assert current_shell_kind(environment, os_name="nt") == "posix"


def test_native_windows_defaults_to_powershell_guidance() -> None:
    assert current_shell_kind({}, os_name="nt") == "powershell"


def test_shell_quoting_and_structured_continuation_round_trip_exact_actor() -> None:
    actor = "O'Brien $HOME; echo nope 🚀"
    assert shlex.split(quote_actor_posix(actor)) == [actor]
    assert quote_actor_powershell(actor) == "'O''Brien $HOME; echo nope 🚀'"

    continuation = actor_continuation(actor, ["anvil", "renew", "C001"])
    assert continuation == {
        "actor": actor,
        "argv": ["anvil", "renew", "C001"],
        "env": {"ANVIL_ACTOR": actor},
        "identity_notice": ACTOR_AUTH_NOTICE,
    }
    assert render_actor_continuation(
        actor, ["anvil", "renew", "C001"], shell="posix"
    ).startswith(f"ANVIL_ACTOR={quote_actor_posix(actor)} ")
    assert (
        render_actor_continuation(actor, ["anvil", "renew", "C001"], shell="powershell")
        == "$env:ANVIL_ACTOR = 'O''Brien $HOME; echo nope 🚀'; & 'anvil' 'renew' 'C001'"
    )


def test_safe_display_redacts_invalid_and_noncanonical_legacy_values() -> None:
    assert safe_actor_display("safe actor") == "safe actor"
    assert safe_actor_display("bad\x00actor").startswith("<unsafe-actor sha256:")
    assert safe_actor_display("Cafe\u0301").startswith("<legacy-actor sha256:")
    assert safe_actor_for_human("safe actor") == "safe actor"
    assert safe_actor_for_human("Cafe\u0301") is None


def test_structured_identity_and_lifecycle_context_never_use_shell_strings() -> None:
    actor = "worker $HOME 'alpha'"
    assert actor_identity_context(actor) == {
        "actor": actor,
        "authenticated": False,
        "notice": ACTOR_AUTH_NOTICE,
    }
    context = continuation_context("T001", "C001", actor)
    assert context["environment"] == {"ANVIL_ACTOR": actor}
    assert context["renew"]["argv"] == [
        "anvil",
        "renew",
        "C001",
        "--actor",
        actor,
    ]
    assert context["release"]["env"] == {"ANVIL_ACTOR": actor}
    assert context["submit"]["argv"][-1] == actor


@pytest.mark.parametrize(
    ("shell_name", "quoted_bundle_id"),
    [
        ("posix", "'B001; echo PWN'"),
        ("nt", "'B001; Write-Output ''PWN'''"),
    ],
)
def test_bundle_continuation_quotes_metacharacter_id_for_current_shell(
    monkeypatch: pytest.MonkeyPatch,
    shell_name: str,
    quoted_bundle_id: str,
) -> None:
    monkeypatch.setattr(
        _actor_output,
        "current_shell_kind",
        lambda: "powershell" if shell_name == "nt" else "posix",
    )

    lines = _actor_output.bundle_continuation_lines(
        "B001; echo PWN" if shell_name == "posix" else "B001; Write-Output 'PWN'",
        "coordinator",
    )

    for action in ("renew", "release", "progress", "complete"):
        rendered = next(line for line in lines if f"bundle {action}" in line)
        assert f"bundle {action} {quoted_bundle_id}" in rendered


def test_bundle_continuation_omits_unsafe_legacy_id_from_human_shell_text() -> None:
    lines = _actor_output.bundle_continuation_lines("B001\x1bPWN", "coordinator")

    assert all("\x1b" not in line for line in lines)
    assert "structured JSON/MCP" in lines[-1]
