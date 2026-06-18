"""Concrete :class:`anvil.sync.provider.SyncProvider` implementations.

Each submodule here defines exactly one provider and auto-registers it
in :data:`anvil.sync.registry.PROVIDER_REGISTRY` at module-load
time. The :mod:`anvil.sync` package's ``__init__`` imports this
package, which is what makes the registrations fire.

External contributor providers (Monday, Linear, Jira, ...) live in
separate packages; they register themselves the same way on their own
module load.
"""

from __future__ import annotations

# Import each provider module so its top-level ``register_sync_provider``
# call executes. Importing ``github_issues`` (and only ``github_issues``
# in v1.8) lands ``"github_issues"`` in the registry as the side effect
# the sync package depends on.
from anvil.sync.providers import github_issues  # noqa: F401

__all__: list[str] = []
