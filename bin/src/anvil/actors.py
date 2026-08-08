"""Actor identity policy and inert continuation rendering.

Actor strings are local coordination and audit identifiers.  They are not
credentials and do not authenticate the caller.  New persisted claim-owner
identities use :func:`canonicalize_new_actor`; lookup paths must compare the
exact value returned by :func:`resolve_actor_input` so historical identities
are never silently normalized or aliased.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Literal

MAX_ACTOR_UTF8_BYTES = 128
ACTOR_AUTH_NOTICE = (
    "Actor identity is local coordination and audit attribution, not cryptographic authentication."
)

_ACTOR_ENV_VARS = ("ANVIL_ACTOR", "ANVIL_GATE_ACTOR")
_BIDI_CONTROLS = frozenset(
    {
        "\u061c",  # ARABIC LETTER MARK
        "\u200e",  # LEFT-TO-RIGHT MARK
        "\u200f",  # RIGHT-TO-LEFT MARK
        "\u202a",  # LEFT-TO-RIGHT EMBEDDING
        "\u202b",  # RIGHT-TO-LEFT EMBEDDING
        "\u202c",  # POP DIRECTIONAL FORMATTING
        "\u202d",  # LEFT-TO-RIGHT OVERRIDE
        "\u202e",  # RIGHT-TO-LEFT OVERRIDE
        "\u2066",  # LEFT-TO-RIGHT ISOLATE
        "\u2067",  # RIGHT-TO-LEFT ISOLATE
        "\u2068",  # FIRST STRONG ISOLATE
        "\u2069",  # POP DIRECTIONAL ISOLATE
    }
)


class ActorIdentityError(ValueError):
    """A proposed new actor identity violates the public identity contract."""


def resolve_actor_input(
    explicit: str | None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return an explicit/environment actor exactly, or ``None`` if absent.

    Presence, rather than truthiness, is intentional.  Lookup operations must
    remain able to address a persisted legacy identity exactly, including one
    that a new claim would now reject.  Creation paths separately call
    :func:`canonicalize_new_actor` before persistence.
    """
    if explicit is not None:
        return explicit
    source = os.environ if environ is None else environ
    for variable in _ACTOR_ENV_VARS:
        if variable in source:
            return source[variable]
    return None


def canonicalize_new_actor(value: str) -> str:
    """Return the NFC actor id accepted for new identity creation.

    The 128-byte limit applies after NFC normalization.  Ordinary spaces,
    Unicode, quotes, and shell metacharacters remain data; rendering helpers
    quote them rather than narrowing the identity alphabet.
    """
    if not isinstance(value, str):
        raise ActorIdentityError("actor identity must be a string")
    try:
        normalized = unicodedata.normalize("NFC", value)
        encoded = normalized.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ActorIdentityError("actor identity contains invalid Unicode") from exc

    if not normalized or not normalized.strip():
        raise ActorIdentityError("actor identity must not be empty or whitespace-only")
    if len(encoded) > MAX_ACTOR_UTF8_BYTES:
        raise ActorIdentityError(f"actor identity exceeds {MAX_ACTOR_UTF8_BYTES} UTF-8 bytes")

    for char in normalized:
        codepoint = ord(char)
        if (
            codepoint <= 0x1F
            or 0x7F <= codepoint <= 0x9F
            or char in {"\u2028", "\u2029"}
            or char in _BIDI_CONTROLS
            or unicodedata.category(char) == "Cs"
        ):
            raise ActorIdentityError(
                f"actor identity contains forbidden code point U+{codepoint:04X}"
            )
    return normalized


def quote_actor_posix(actor: str) -> str:
    """Return ``actor`` as one inert POSIX shell word."""
    return shlex.quote(actor)


def quote_actor_powershell(actor: str) -> str:
    """Return ``actor`` as one inert PowerShell single-quoted string."""
    return "'" + actor.replace("'", "''") + "'"


# Compatibility names used by presentation adapters.  Keeping the actor noun
# last reads naturally at those call sites while the shorter public names above
# remain convenient for direct use.
quote_posix_actor = quote_actor_posix
quote_powershell_actor = quote_actor_powershell


def actor_continuation(actor: str, argv: Sequence[str]) -> dict[str, object]:
    """Return an exact, shell-free argv/env continuation for JSON or MCP."""
    return {
        "actor": actor,
        "argv": list(argv),
        "env": {"ANVIL_ACTOR": actor},
        "identity_notice": ACTOR_AUTH_NOTICE,
    }


def actor_identity_context(actor: str) -> dict[str, object]:
    """Return exact identity metadata suitable for a structured response."""
    return {
        "actor": actor,
        "authenticated": False,
        "notice": ACTOR_AUTH_NOTICE,
    }


def continuation_context(
    task_id: str,
    claim_id: str,
    actor: str,
) -> dict[str, object]:
    """Return exact argv/env continuations for a newly acquired claim."""
    return {
        "environment": {"ANVIL_ACTOR": actor},
        "renew": actor_continuation(actor, ["anvil", "renew", claim_id, "--actor", actor]),
        "release": actor_continuation(actor, ["anvil", "release", claim_id, "--actor", actor]),
        "submit": actor_continuation(actor, ["anvil", "submit", task_id, "--actor", actor]),
        "identity_notice": ACTOR_AUTH_NOTICE,
    }


def render_actor_continuation(
    actor: str,
    argv: Sequence[str],
    *,
    shell: Literal["posix", "powershell"],
) -> str:
    """Render a platform-specific command carrying the exact actor value."""
    if shell == "posix":
        command = " ".join(shlex.quote(argument) for argument in argv)
        return f"ANVIL_ACTOR={quote_actor_posix(actor)} {command}"
    assignments = f"$env:ANVIL_ACTOR = {quote_actor_powershell(actor)}"
    command = " ".join(quote_actor_powershell(argument) for argument in argv)
    return f"{assignments}; & {command}"


def safe_actor_display(actor: str) -> str:
    """Return valid actor text, or a stable redacted label for legacy-unsafe data."""
    try:
        canonical = canonicalize_new_actor(actor)
    except ActorIdentityError:
        digest = hashlib.sha256(actor.encode("utf-8", errors="backslashreplace")).hexdigest()[:12]
        return f"<unsafe-actor sha256:{digest}>"
    if canonical != actor:
        digest = hashlib.sha256(actor.encode("utf-8", errors="backslashreplace")).hexdigest()[:12]
        return f"<legacy-actor sha256:{digest}>"
    return actor


def safe_actor_for_human(actor: str) -> str | None:
    """Return an actor safe to interpolate as inert text, else ``None``.

    A non-NFC legacy value is intentionally not rewritten for display: showing
    a normalized lookalike would imply an alias that exact ownership checks do
    not recognize.
    """
    try:
        return actor if canonicalize_new_actor(actor) == actor else None
    except ActorIdentityError:
        return None
