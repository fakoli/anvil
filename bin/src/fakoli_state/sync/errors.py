"""Exception hierarchy for the fakoli-state sync subsystem (Phase 8 Wave 1).

Single base class :class:`SyncProviderError` so callers (CLI, reconciliation
engine, MCP tools) can wrap every provider interaction with a single
``except SyncProviderError`` and surface a clean user-facing error.

Mirrors the Phase 7 :class:`fakoli_state.planning.llm.LLMProviderError`
pattern: every underlying SDK / HTTP / subprocess / lookup failure inside a
:class:`fakoli_state.sync.provider.SyncProvider` implementation MUST be
wrapped via ``raise SyncProviderError(...) from exc`` (chain the original).

The leaf subclasses below exist so callers that *do* care about a specific
failure mode (e.g. the CLI wants to print a different message on auth-vs-
rate-limit) can ``except`` the narrower type; everyone else catches the
base and gets a uniform error.
"""

from __future__ import annotations

__all__ = [
    "SyncProviderError",
    "SyncConflict",
    "ProviderUnavailable",
    "AuthenticationFailed",
    "RateLimitExceeded",
]


class SyncProviderError(Exception):
    """Base class for every sync-provider failure.

    Implementations of :class:`fakoli_state.sync.provider.SyncProvider` MUST
    wrap lower-level errors in this class (or a subclass below) and chain
    the original via ``raise SyncProviderError(...) from exc``. Callers can
    then ``except SyncProviderError`` once and recover the underlying cause
    via ``exc.__cause__`` for logging / debugging.
    """


class SyncConflict(SyncProviderError):
    """Local and remote state diverged in a way the provider cannot reconcile.

    Raised by ``push_task`` / ``fetch_task`` when both sides changed since
    the last sync and the configured conflict-resolution strategy refuses
    to silently overwrite (e.g. ``manual_merge``). The reconciliation
    engine (Task 5) catches this and surfaces it to the user.
    """


class ProviderUnavailable(SyncProviderError):
    """The upstream system is unreachable for transport-level reasons.

    Network down, DNS failure, HTTP 5xx, subprocess (``gh``) not on PATH,
    etc. Distinct from :class:`AuthenticationFailed` (credentials wrong)
    and :class:`RateLimitExceeded` (we're throttled). Callers may retry
    with backoff.
    """


class AuthenticationFailed(SyncProviderError):
    """The provider rejected our credentials.

    HTTP 401/403, ``gh auth status`` failure, missing/expired token.
    Retrying with the same credentials will not help — the user must
    re-authenticate.
    """


class RateLimitExceeded(SyncProviderError):
    """The provider is throttling us.

    HTTP 429, GitHub primary/secondary rate-limit headers, etc. Callers
    should back off (the duration depends on the provider's policy; the
    CLI prints the wrapped message).
    """
