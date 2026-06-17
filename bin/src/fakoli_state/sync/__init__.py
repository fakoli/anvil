"""fakoli-state.sync — bidirectional sync abstraction (Phase 8).

This package defines the :class:`SyncProvider` Protocol that every
bidirectional-sync backend (GitHub Issues v0, Monday / Linear / Jira /
GitHub Projects future) implements, plus the registry that wires them
into the CLI by name.

Public surface (re-exported here for ergonomic ``from fakoli_state.sync
import ...``):

- :class:`SyncProvider` — the Protocol.
- :class:`ExternalRef` — minimal pointer to a remote record.
- :class:`ExternalTask` — full remote payload returned by fetch/list.
- :class:`ProviderHealth` — diagnostic snapshot.
- :class:`RecordedSyncProvider` — deterministic test double.
- :class:`SyncProviderError` + subclasses — single exception hierarchy.
- :func:`register_sync_provider`, :func:`get_sync_provider`,
  :func:`list_sync_providers` — registry interface.
- :data:`PROVIDER_REGISTRY` — underlying dict, exposed for introspection.

This file does NOT import any concrete provider (GitHubIssuesProvider is
Task 4 and lives in ``fakoli_state.sync.providers.github_issues``); pulling
a provider in here would defeat the registry pattern (the whole point is
that providers register themselves on their own module load).
"""

from __future__ import annotations

from fakoli_state.sync.errors import (
    AuthenticationFailed,
    ProviderUnavailable,
    RateLimitExceeded,
    SyncConflict,
    SyncProviderError,
)
from fakoli_state.sync.provider import (
    ExternalRef,
    ExternalTask,
    ProviderHealth,
    SyncProvider,
)
from fakoli_state.sync.recorded import RecordedSyncProvider
from fakoli_state.sync.registry import (
    PROVIDER_REGISTRY,
    get_sync_provider,
    list_sync_providers,
    register_sync_provider,
)

__all__ = [
    # Protocol + payloads
    "SyncProvider",
    "ExternalRef",
    "ExternalTask",
    "ProviderHealth",
    # Test double
    "RecordedSyncProvider",
    # Errors
    "SyncProviderError",
    "SyncConflict",
    "ProviderUnavailable",
    "AuthenticationFailed",
    "RateLimitExceeded",
    # Registry
    "PROVIDER_REGISTRY",
    "register_sync_provider",
    "get_sync_provider",
    "list_sync_providers",
]

# Side-effect import: loads providers package, which imports each provider
# module, which calls register_sync_provider at module top level. This is
# the ONE place the package wires concrete providers in — keeps the
# registry pattern clean and the rest of the package provider-agnostic.
from fakoli_state.sync import providers as _providers  # noqa: E402, F401
