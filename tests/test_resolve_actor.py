"""B47 — the unified actor resolver (`resolve_actor`).

One identity across claim / heartbeat / gate-check / claim-guard so a claim made
under one actor is never heartbeated or gated under another (which would renew
zero leases and fail the finish-gate silently OPEN).
"""

from __future__ import annotations

import pytest

from anvil.cli._helpers import resolve_actor

_ENV_VARS = (
    "ANVIL_ACTOR", "ANVIL_GATE_ACTOR", "USER",
    "ANVIL_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_explicit_actor_wins_and_is_stripped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ANVIL_ACTOR", "env-actor")
    assert resolve_actor("explicit") == "explicit"
    assert resolve_actor("  spaced  ") == "spaced"


def test_anvil_actor_env_beats_legacy_and_user(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("ANVIL_GATE_ACTOR", "legacy")
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("ANVIL_ACTOR", "fleet-1")
    # explicit None or empty falls through to the env tiers
    assert resolve_actor(None) == "fleet-1"
    assert resolve_actor("") == "fleet-1"
    assert resolve_actor("   ") == "fleet-1"


def test_legacy_gate_actor_env_still_honored(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("ANVIL_GATE_ACTOR", "legacy-gate")
    assert resolve_actor(None) == "legacy-gate"


def test_user_preferred_over_fingerprint(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("USER", "alice")
    assert resolve_actor(None) == "alice"


def test_fingerprint_fallback_is_stable_and_not_agent(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("ANVIL_KEYS_DIR", str(tmp_path / "keys"))
    first = resolve_actor(None)
    second = resolve_actor(None)  # stable: loads the same keypair from disk
    assert first == second
    assert first != "agent"
    assert len(first) == 16
    assert all(c in "0123456789abcdef" for c in first)


def test_agent_is_the_last_resort_when_signing_unavailable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    import anvil.signing as signing

    def _boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("no key dir")

    monkeypatch.setattr(signing, "load_or_create_signer", _boom)
    assert resolve_actor(None) == "agent"


# ---------------------------------------------------------------------------
# B47/#103 — a per-loop session discriminator on the DERIVED default identity so
# concurrent sibling loops are distinguishable by default.
# ---------------------------------------------------------------------------


def test_session_id_disambiguates_derived_identity(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("ANVIL_SESSION_ID", "0123456789abcdef")
    # The session id is sliced to 12 chars and appended to the derived base.
    assert resolve_actor(None) == "alice-0123456789ab"


def test_two_sessions_yield_distinct_actors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The #103 fix: two concurrent loops (same user, different session) resolve
    DIFFERENT actors, so the second claim conflicts instead of renewing."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("ANVIL_SESSION_ID", "loop-aaaa")
    a = resolve_actor(None)
    monkeypatch.setenv("ANVIL_SESSION_ID", "loop-bbbb")
    b = resolve_actor(None)
    assert a != b


def test_same_session_yields_same_actor(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """In-loop agreement (B47): repeated resolution within one loop is stable."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("ANVIL_SESSION_ID", "loop-aaaa")
    assert resolve_actor(None) == resolve_actor(None)


def test_anvil_session_id_preferred_over_claude_code(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-xxxx")
    monkeypatch.setenv("ANVIL_SESSION_ID", "anvil-yyyy")
    assert resolve_actor(None) == "alice-anvil-yyyy"


def test_claude_code_session_id_used_as_fallback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-xxxx")
    assert resolve_actor(None) == "alice-claude-xxxx"


def test_no_session_leaves_derived_identity_bare(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("USER", "alice")
    assert resolve_actor(None) == "alice"


def test_explicit_and_anvil_actor_ignore_session(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The coordination knobs are returned verbatim — never session-suffixed."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("ANVIL_SESSION_ID", "loop-aaaa")
    assert resolve_actor("pinned") == "pinned"
    monkeypatch.setenv("ANVIL_ACTOR", "fleet-1")
    assert resolve_actor(None) == "fleet-1"


def test_gate_actor_is_not_session_suffixed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """$ANVIL_GATE_ACTOR (OpenClaw's knob) stays verbatim so existing native
    installs are unaffected by the session discriminator."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("ANVIL_GATE_ACTOR", "legacy-gate")
    monkeypatch.setenv("ANVIL_SESSION_ID", "loop-aaaa")
    assert resolve_actor(None) == "legacy-gate"
