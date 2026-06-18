"""Stale-claim detector and reaper.

detect_and_release_stale() is idempotent: safe to call on every CLI invocation
and from the Phase 6 MCP server without coordination.

Event actions emitted (welder maps to SQL handlers):

claim.stale payload_json:
  {
    "claim_id": str,
    "task_id": str,
    "expired_at": str,        # claim.lease_expires_at ISO 8601 UTC
    "detected_at": str,       # clock.now() ISO 8601 UTC
    "actor": str,             # "system" by default
  }

The claim.stale handler (welder) must:
  1. SET claims.status = 'stale' WHERE id = claim_id AND status = 'active'
  2. Emit or directly apply task.status_changed (claimed/in_progress → stale → ready)
     — either as a compound handler or by routing to _handle_task_status_changed twice.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from anvil.clock import Clock
from anvil.state.models import ClaimStatus, EventDraft

if TYPE_CHECKING:
    from anvil.state.backend import Backend

logger = logging.getLogger(__name__)


def detect_and_release_stale(
    backend: Backend,
    clock: Clock,
    *,
    actor: str = "system",
) -> list[str]:
    """Scan active claims and emit claim.stale for any with an expired lease.

    Idempotent: claims already in 'stale' status are returned by
    list_active_claims() only if that method filters on status='active'
    (which the SqliteBackend implementation does).  Re-running this function
    after welder's handler has transitioned the claim to 'stale' will
    therefore naturally skip already-reaped claims.

    A per-claim try/except ensures one bad claim (e.g. its task was already
    deleted by a concurrent operation) does not prevent the others from being
    reaped.

    Args:
        backend: Backend instance to query and mutate via append().
        clock:   Clock instance — all timestamp generation goes through this.
        actor:   Identity for the emitted events (default: "system").

    Returns:
        List of claim IDs that were marked stale in this invocation.
    """
    now = clock.now()
    active_claims = backend.list_active_claims()
    reaped: list[str] = []

    for claim in active_claims:
        if claim.status != ClaimStatus.active:
            # Defensive guard: list_active_claims() should only return active
            # claims, but we guard here for safety so future backend impls
            # that widen the filter don't cause double-reaping.
            continue

        if claim.lease_expires_at >= now:
            # Lease still valid; skip.
            continue

        try:
            stale_draft = EventDraft(
                timestamp=now,
                actor=actor,
                action="claim.stale",
                target_kind="claim",
                target_id=claim.id,
                payload_json={
                    "claim_id": claim.id,
                    "task_id": claim.task_id,
                    "expired_at": claim.lease_expires_at.isoformat(),
                    "detected_at": now.isoformat(),
                    "reason": "lease_expired",
                    "actor": actor,
                },
            )
            backend.append(stale_draft)
            reaped.append(claim.id)
            logger.info(
                "Reaped stale claim %r (task %r, expired %s)",
                claim.id,
                claim.task_id,
                claim.lease_expires_at.isoformat(),
            )
        except Exception:
            logger.exception(
                "Failed to reap stale claim %r (task %r); skipping and continuing",
                claim.id,
                claim.task_id,
            )

    return reaped


