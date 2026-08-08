"""Shared user-facing actor metadata and safe lifecycle guidance.

The actor policy itself lives in :mod:`anvil.actors`.  This module only adapts
that policy to CLI/MCP output shapes so every lifecycle command uses the same
notice, structured remedies, and platform-specific human rendering.
"""

from __future__ import annotations

from typing import Any

from anvil.actors import (
    ACTOR_AUTH_NOTICE,
    actor_identity_context,
    bundle_continuation_context,
    continuation_context,
    current_shell_kind,
    quote_posix_actor,
    quote_powershell_actor,
    safe_actor_for_human,
)


def actor_identity_data(actor: str) -> dict[str, Any]:
    """Return the canonical additive actor-identity response object."""
    return actor_identity_context(actor)


def continuation_data(
    task_id: str,
    claim_id: str,
    actor: str,
    *,
    attestation_context: dict[str, Any] | None = None,
    generation: int | None = None,
) -> dict[str, Any]:
    """Return structured argv/environment data for lifecycle continuation."""
    return dict(
        continuation_context(
            task_id,
            claim_id,
            actor,
            attestation_context=attestation_context,
            generation=generation,
        )
    )


def bundle_continuation_data(bundle_id: str, actor: str) -> dict[str, Any]:
    """Return bundle-specific lifecycle argv without a task-submit command."""
    return dict(bundle_continuation_context(bundle_id, actor))


def _quote_for_current_shell(value: str) -> str:
    return (
        quote_powershell_actor(value)
        if current_shell_kind() == "powershell"
        else quote_posix_actor(value)
    )


def actor_flag_for_human(actor: str) -> str | None:
    """A current-platform ``--actor`` fragment, or None for unsafe legacy IDs."""
    if safe_actor_for_human(actor) is None:
        return None
    return f"--actor {_quote_for_current_shell(actor)}"


def actor_env_for_human(actor: str) -> str | None:
    """A current-platform ANVIL_ACTOR assignment, or None when unsafe."""
    if safe_actor_for_human(actor) is None:
        return None
    quoted = _quote_for_current_shell(actor)
    if current_shell_kind() == "powershell":
        return f"$env:ANVIL_ACTOR = {quoted}"
    return f"export ANVIL_ACTOR={quoted}"


def hook_env_for_human(actor: str, claim_id: str) -> str | None:
    """Current-shell hook attribution pins, or None for an unsafe actor."""
    if safe_actor_for_human(actor) is None:
        return None
    actor_quoted = _quote_for_current_shell(actor)
    claim_quoted = _quote_for_current_shell(claim_id)
    if current_shell_kind() == "powershell":
        return (
            f"$env:ANVIL_ACTOR = {actor_quoted}; "
            f"$env:ANVIL_CLAIM_ID = {claim_quoted}"
        )
    return f"export ANVIL_ACTOR={actor_quoted} ANVIL_CLAIM_ID={claim_quoted}"


def safe_actor_label(actor: str) -> str:
    """Render a human label without ever exposing unsafe persisted bytes."""
    safe = safe_actor_for_human(actor)
    return f"'{safe}'" if safe is not None else "<unsafe legacy actor; use --json>"


def actor_mismatch_message(*, owner: str, actual: str, action: str) -> str:
    """Human-safe wrong-owner refusal with an exact current-shell remedy."""
    owner_safe = safe_actor_for_human(owner)
    actual_safe = safe_actor_for_human(actual)
    if owner_safe is None:
        return (
            f"{action} refused: the persisted owner is unsafe to render in shell text. "
            "Rerun with --json or use the MCP structured actor field for the exact "
            "owner and remediation; no ownership change was made."
        )
    owner_flag = actor_flag_for_human(owner)
    owner_env = actor_env_for_human(owner)
    actual_label = (
        f"'{actual_safe}'" if actual_safe is not None else "an unsafe actor value"
    )
    return (
        f"{action} refused: claim owner is '{owner_safe}', not {actual_label}. "
        f"Retry with {owner_flag}, or pin the owner with `{owner_env}`. "
        "No ownership change was made."
    )


def actor_mismatch_data(*, owner: str, actual: str) -> dict[str, Any]:
    """Structured exact-owner remedies for JSON/MCP callers."""
    return {
        "owner": owner,
        "resolved_actor": actual,
        "remedies": {
            "actor_argv": ["--actor", owner],
            "environment": {"ANVIL_ACTOR": owner},
        },
        "authenticated": False,
        "notice": ACTOR_AUTH_NOTICE,
    }


def actor_notice_lines(actor: str) -> list[str]:
    """Compact human footer shared by successful lifecycle commands."""
    lines = [f"  Actor:        {safe_actor_label(actor)}", f"  {ACTOR_AUTH_NOTICE}"]
    env_line = actor_env_for_human(actor)
    if env_line is not None:
        lines.append(f"  Pin for continuation: `{env_line}`")
    else:
        lines.append(
            "  Actor cannot be rendered safely in shell text; use --json or MCP "
            "structured fields to continue."
        )
    return lines


def bundle_continuation_lines(bundle_id: str, actor: str) -> list[str]:
    """Render one safe human bundle lifecycle handoff for the current shell."""
    lines = actor_notice_lines(actor)
    actor_flag = actor_flag_for_human(actor)
    if actor_flag is None or safe_actor_for_human(bundle_id) is None:
        lines.append("  Use the structured JSON/MCP bundle continuation argv.")
        return lines
    quoted_bundle_id = _quote_for_current_shell(bundle_id)
    lines.extend(
        [
            f"  Renew:    `anvil bundle renew {quoted_bundle_id} {actor_flag}`",
            f"  Release:  `anvil bundle release {quoted_bundle_id} {actor_flag}`",
            f"  Progress: `anvil bundle progress {quoted_bundle_id} PHASE {actor_flag}`",
            f"  Complete: `anvil bundle complete {quoted_bundle_id} {actor_flag}`",
        ]
    )
    return lines
