"""Transport clients for the GitHub Issues sync provider (Phase 8 Wave 2).

Two interchangeable clients implement the same internal call shape so the
provider can pick the best transport at runtime:

- :mod:`anvil.sync.clients.gh_cli` — wraps the ``gh`` CLI via
  ``subprocess.run``. Reuses the user's existing ``gh auth`` session, so no
  PAT plumbing is required when ``gh`` is installed.
- :mod:`anvil.sync.clients.github_http` — direct ``httpx``-based REST
  client. Reads ``GITHUB_TOKEN`` from the environment. Used when ``gh`` is
  not on PATH or not authenticated.

Both clients raise :class:`anvil.sync.errors.SyncProviderError`
(or one of its leaf subclasses) on every failure mode the
:class:`anvil.sync.provider.SyncProvider` Protocol expects callers
to be able to handle. They never raise ``subprocess.CalledProcessError``
or ``httpx.HTTPError`` directly — those become
:class:`anvil.sync.errors.ProviderUnavailable` /
:class:`AuthenticationFailed` / :class:`RateLimitExceeded` /
:class:`SyncProviderError`.
"""

from __future__ import annotations

__all__: list[str] = []
