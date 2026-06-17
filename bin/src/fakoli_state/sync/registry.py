"""Provider registry for the fakoli-state sync subsystem (Phase 8 Wave 1).

External contributors register their :class:`SyncProvider` implementations
by importing their module (which calls :func:`register_sync_provider` at
import time) and the CLI resolves ``fakoli-state sync <provider_id>`` via
:func:`get_sync_provider`.

The built-in GitHub Issues provider (Task 4) will register itself on its
module load. This module ships with an empty registry — registration is
the responsibility of each provider module, never this file. That keeps
the registry from becoming a coupling point that has to know every
provider that exists.

Public surface
--------------
- :data:`PROVIDER_REGISTRY` — the underlying ``{provider_id: cls}`` dict.
  Exposed for introspection (tests, ``--list-providers`` CLI), not for
  direct mutation; callers SHOULD use the functions below so duplicate
  registrations fail loudly.
- :func:`register_sync_provider` — register a provider class under its id.
- :func:`get_sync_provider` — look up a provider class by id.
- :func:`list_sync_providers` — sorted list of all registered ids.
"""

from __future__ import annotations

from fakoli_state.sync.provider import SyncProvider

__all__ = [
    "PROVIDER_REGISTRY",
    "register_sync_provider",
    "get_sync_provider",
    "list_sync_providers",
]


# The underlying registry. Module-level so registration survives any single
# caller's lifetime; built-in providers register on first import of their
# module, contributors do the same.
PROVIDER_REGISTRY: dict[str, type[SyncProvider]] = {}


def register_sync_provider(provider_id: str, cls: type[SyncProvider]) -> None:
    """Register ``cls`` under ``provider_id``.

    ``provider_id`` SHOULD be snake_case to match the
    :class:`fakoli_state.state.models.ExternalSystem` enum and the
    ``sync_mappings.external_system`` DB column shape (e.g.
    ``"github_issues"``, NOT ``"github-issues"``). The registry does not
    enforce casing — kebab-case strings register and look up fine — but
    kebab keys will collide with no SyncMapping row at storage time and
    produce confusing reconciliation failures downstream.

    Raises
    ------
    ValueError
        If ``provider_id`` is empty, or if a provider is already registered
        under this id. Duplicate registration is a hard error rather than
        a silent overwrite — silent overwrites are how plugins shadow each
        other in production and waste hours of debugging.
    """
    if not provider_id:
        raise ValueError("provider_id must be a non-empty string")
    if provider_id in PROVIDER_REGISTRY:
        existing = PROVIDER_REGISTRY[provider_id]
        raise ValueError(
            f"provider_id {provider_id!r} is already registered to "
            f"{existing.__module__}.{existing.__name__}; refusing to "
            f"overwrite with {cls.__module__}.{cls.__name__}"
        )
    PROVIDER_REGISTRY[provider_id] = cls


def get_sync_provider(provider_id: str) -> type[SyncProvider]:
    """Return the provider class registered under ``provider_id``.

    Raises
    ------
    KeyError
        If no provider is registered under this id. The message lists the
        currently-registered ids so the CLI can surface a helpful "did you
        mean one of [...]" hint.
    """
    if provider_id not in PROVIDER_REGISTRY:
        available = ", ".join(sorted(PROVIDER_REGISTRY)) or "(none)"
        raise KeyError(
            f"no sync provider registered under {provider_id!r}; "
            f"available providers: {available}"
        )
    return PROVIDER_REGISTRY[provider_id]


def list_sync_providers() -> list[str]:
    """Return a sorted list of every registered provider id.

    Sorted for deterministic CLI output (so ``fakoli-state sync --list``
    is stable across runs). Returns a fresh list; callers may mutate it.
    """
    return sorted(PROVIDER_REGISTRY)
