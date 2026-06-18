"""Sync provider Protocol + payload models (Phase 8 Wave 1).

This module is the *only* place anvil defines the abstract
bidirectional-sync surface. Concrete implementations (GitHub Issues in
Task 4; Monday / Linear / Jira in future contributor PRs) implement the
:class:`SyncProvider` Protocol and register themselves in
:mod:`anvil.sync.registry`.

Public surface
--------------
- :class:`ExternalRef` — minimal pointer to a remote record (provider id +
  external id + optional URL). Stored on a ``SyncMapping`` row (Task 2) so
  the local task knows where its remote twin lives.
- :class:`ExternalTask` — the payload returned by ``fetch_task`` / ``list_tasks``.
  Everything the remote knows about the task; the reconciliation engine
  diffs it against the local :class:`anvil.state.models.Task`.
- :class:`ProviderHealth` — diagnostic snapshot returned by ``health_check``.
- :class:`SyncProvider` — the Protocol every backend implements.

Design notes
------------
* Methods are **keyword-only after self**. Same discipline as
  :class:`anvil.planning.llm.LLMProvider`: positional args break the
  moment we add an option, and the abstraction is on the hot path for
  contributor PRs (every new provider re-types these signatures), so the
  noisy ``*, ...`` boundary is worth the line length.
* Pydantic models use ``ConfigDict(extra="forbid")`` so a provider that
  silently adds an unexpected field gets caught at the abstraction
  boundary instead of bleeding garbage into ``SyncMapping`` storage.
* Single exception type to catch: :class:`anvil.sync.errors.SyncProviderError`.
  Every implementation MUST wrap underlying errors with ``raise ... from exc``.
* The Protocol uses :class:`typing.Protocol` (NOT ``ABC``) — runtime
  structural typing, same as the Phase 7 LLM stack. We do not decorate
  with ``@runtime_checkable``: the structural check is a static-analysis
  contract, not an ``isinstance()`` check.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    # Type-only import: the Task entity lives in state.models. We only need
    # the type for the push_task signature; importing it eagerly would pull
    # the whole state stack into the sync abstraction layer, which is the
    # wrong direction architecturally (sync depends on state, not vice
    # versa, but the *abstraction* shouldn't import a concrete entity at
    # module load when only the signature needs it).
    from anvil.state.models import Task

__all__ = [
    "ExternalRef",
    "ExternalTask",
    "ProviderHealth",
    "SyncProvider",
]


# ---------------------------------------------------------------------------
# Shared model config — matches state.models discipline
# ---------------------------------------------------------------------------


_MODEL_CONFIG = ConfigDict(
    frozen=False,
    validate_assignment=True,
    extra="forbid",
)


def _require_utc(dt: datetime.datetime, field_name: str) -> datetime.datetime:
    """Raise ValueError if dt is naive (no tzinfo).

    Mirrors :func:`anvil.state.models._require_utc` so all timestamps
    in anvil — local entities AND remote payloads — share the same
    UTC-or-die contract. A naive datetime from a provider would otherwise
    silently round-trip through SQLite and cause off-by-tz drift later.
    """
    if dt.tzinfo is None:
        raise ValueError(
            f"{field_name} must be timezone-aware (UTC); "
            f"got naive datetime {dt!r}. "
            "Use datetime.datetime.now(datetime.timezone.utc) or "
            "datetime.datetime(..., tzinfo=datetime.timezone.utc)."
        )
    return dt


# ---------------------------------------------------------------------------
# ExternalRef — pointer to a remote record
# ---------------------------------------------------------------------------


class ExternalRef(BaseModel):
    """Minimal pointer to a record on a remote sync target.

    Stored on a ``SyncMapping`` row (Task 2) so the local task knows where
    its remote twin lives. Deliberately small — three fields, no payload
    data. Heavy payload lives in :class:`ExternalTask`, which is recomputed
    on every ``fetch_task`` call.
    """

    model_config = _MODEL_CONFIG

    provider_id: str = Field(
        min_length=1,
        description=(
            "Registry key of the provider that owns this ref, e.g. "
            "``github_issues`` (snake_case — matches the "
            ":class:`anvil.state.models.ExternalSystem` enum and the "
            "DB column shape). Must match a key in "
            ":data:`anvil.sync.registry.PROVIDER_REGISTRY`."
        ),
    )
    external_id: str = Field(
        min_length=1,
        description=(
            "Provider-native record id. For ``github_issues`` this is the "
            "stringified issue number (e.g. ``\"42\"``); for Linear it would "
            "be the issue identifier (``\"ENG-123\"``); etc. Always a string "
            "so the table column type is uniform across providers."
        ),
    )
    url: str | None = Field(
        default=None,
        description=(
            "Optional human-facing URL to the record (issue page, etc.). "
            "Convenience for CLI output and reconciliation reports; not "
            "load-bearing — providers that don't expose a URL set None."
        ),
    )


# ---------------------------------------------------------------------------
# ExternalTask — full payload returned by fetch/list
# ---------------------------------------------------------------------------


class ExternalTask(BaseModel):
    """What the remote knows about a task right now.

    Returned by :meth:`SyncProvider.fetch_task` and
    :meth:`SyncProvider.list_tasks`. The reconciliation engine (Task 5)
    diffs this against the local :class:`anvil.state.models.Task`
    to decide whether the local copy, remote copy, or both need updating.

    Field shape choices
    -------------------
    * ``status_label`` is the **provider-native** status string (e.g.
      ``"open"``, ``"closed"`` for GitHub; ``"In Progress"`` for Linear).
      Mapping to anvil's :class:`anvil.state.models.TaskStatus`
      lives inside the provider implementation, not on this payload — the
      Protocol stays generic across providers with wildly different status
      vocabularies.
    * Provider-specific extras (GitHub labels / assignees, Jira watchers /
      reporter, Monday people columns, ...) live in :attr:`provider_metadata`
      as an opaque dict. Baking provider-shaped fields onto the Protocol
      would lock the abstraction into one vendor's data shape; pushing
      them through a generic dict keeps the Protocol provider-agnostic.
    """

    model_config = _MODEL_CONFIG

    external_id: str = Field(
        min_length=1,
        description="Provider-native id, same shape as :attr:`ExternalRef.external_id`.",
    )
    title: str = Field(
        description=(
            "Remote title. Empty string is allowed (some providers permit "
            "blank titles); ``None`` is not, so callers don't need ``or ''``."
        ),
    )
    body: str = Field(
        default="",
        description=(
            "Remote body / description text. Empty string for records with "
            "no body. Markdown-flavoured for every provider we currently "
            "target; no rendering happens at this layer."
        ),
    )
    status_label: str | None = Field(
        default=None,
        description=(
            "Provider-native status string (e.g. ``\"open\"``, ``\"closed\"``, "
            "``\"In Progress\"``). ``None`` when the provider has no status "
            "field at all (rare). Mapping to anvil's TaskStatus enum "
            "lives in the provider implementation."
        ),
    )
    url: str | None = Field(
        default=None,
        description="Human-facing URL, same semantics as :attr:`ExternalRef.url`.",
    )
    last_modified: datetime.datetime = Field(
        description=(
            "When the remote record was last updated, per the provider's "
            "own clock. MUST be timezone-aware UTC. Drives conflict "
            "detection in the reconciliation engine."
        ),
    )
    provider_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Provider-specific extension blob. GitHub puts "
            "``{\"labels\": [...], \"assignees\": [...]}`` here; "
            "Jira puts ``{\"watchers\": [...], \"reporter\": ...}``; "
            "Monday puts people-column shapes. The reconciliation engine "
            "treats this as opaque — only the originating provider knows "
            "the shape. Mirrors :attr:`anvil.state.models.SyncMapping"
            ".provider_metadata` so a fetch-then-persist round trip is lossless."
        ),
    )

    @field_validator("last_modified", mode="after")
    @classmethod
    def _validate_last_modified_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "last_modified")


# ---------------------------------------------------------------------------
# ProviderHealth — diagnostic snapshot
# ---------------------------------------------------------------------------


class ProviderHealth(BaseModel):
    """Snapshot of provider reachability + credential state.

    Returned by :meth:`SyncProvider.health_check`. The CLI's ``sync --help``
    / ``sync <provider> status`` surface and the reconciliation engine
    both call this to decide whether attempting a sync is even worth it
    before touching the network.
    """

    model_config = _MODEL_CONFIG

    available: bool = Field(
        description=(
            "True iff the provider can currently reach the upstream system. "
            "Independent of auth — auth-failed-with-network-up is "
            "``available=True, auth_configured=False``."
        ),
    )
    auth_configured: bool = Field(
        description=(
            "True iff the provider has valid credentials. For "
            "``github_issues`` this means either ``gh auth status`` "
            "succeeds OR ``GITHUB_TOKEN`` is set and accepted."
        ),
    )
    last_check_at: datetime.datetime = Field(
        description="When this health snapshot was taken (UTC).",
    )
    error: str | None = Field(
        default=None,
        description=(
            "Human-readable explanation when ``available`` or "
            "``auth_configured`` is False. ``None`` on a fully healthy "
            "provider. Surfaced verbatim by the CLI; keep it short."
        ),
    )

    @field_validator("last_check_at", mode="after")
    @classmethod
    def _validate_last_check_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _require_utc(v, "last_check_at")


# ---------------------------------------------------------------------------
# SyncProvider Protocol
# ---------------------------------------------------------------------------


class SyncProvider(Protocol):
    """Abstract bidirectional sync target.

    Implementations:
    - :class:`anvil.sync.recorded.RecordedSyncProvider` (test double, this package)
    - ``GitHubIssuesProvider`` (Task 4, ``sync.providers.github_issues``)
    - Future: Monday, Linear, Jira, GitHub Projects, ...

    Contract
    --------
    * All methods are keyword-only after ``self``. Same discipline as
      :class:`anvil.planning.llm.LLMProvider`.
    * Every method MUST wrap underlying failures in
      :class:`anvil.sync.errors.SyncProviderError` (or a subclass)
      via ``raise ... from exc``. Callers ``except SyncProviderError`` once.
    * Implementations SHOULD be safe to call repeatedly with the same
      arguments — sync is a polling loop, idempotency matters.
    * The ``provider_id`` / ``display_name`` attributes are class-level
      identifiers; the registry uses ``provider_id`` as its key. Both are
      plain strings (not enums) so external contributors can add providers
      without patching this package.
    """

    provider_id: str
    """Registry key (e.g. ``\"github_issues\"``, ``\"monday\"``, ``\"linear\"``)."""

    display_name: str
    """Human-facing name for CLI output (e.g. ``\"GitHub Issues\"``)."""

    def push_task(
        self,
        *,
        task: Task,
        mapping: ExternalRef | None,
    ) -> ExternalRef:
        """Create or update the remote record for ``task``.

        Parameters
        ----------
        task:
            The local :class:`anvil.state.models.Task` to push.
        mapping:
            Existing :class:`ExternalRef` if a remote record already exists
            for this task (from a prior sync); ``None`` for a first push.
            Implementations branch on this: ``None`` → create, otherwise → update.

        Returns
        -------
        ExternalRef
            Pointer to the remote record. Caller persists this in the
            ``SyncMapping`` table (Task 2).

        Raises
        ------
        anvil.sync.errors.SyncProviderError
            On any upstream failure.
        """
        ...  # pragma: no cover — Protocol

    def fetch_task(self, *, external_id: str) -> ExternalTask | None:
        """Pull the current remote payload for ``external_id``.

        Parameters
        ----------
        external_id:
            Provider-native id (see :attr:`ExternalRef.external_id`).

        Returns
        -------
        ExternalTask | None
            The current remote payload, or ``None`` if the record no longer
            exists (deleted upstream). Callers treat ``None`` as a tombstone
            signal — the reconciliation engine may then drop the local
            ``SyncMapping`` row.

        Raises
        ------
        anvil.sync.errors.SyncProviderError
            On any upstream failure other than a clean 404 / missing record.
        """
        ...  # pragma: no cover — Protocol

    def list_tasks(self) -> list[ExternalTask]:
        """List every remote record visible to this provider.

        Returns
        -------
        list[ExternalTask]
            All records in the configured scope (e.g. all issues in a
            single GitHub repo). Implementations handle pagination
            transparently; callers receive the full list.

        Raises
        ------
        anvil.sync.errors.SyncProviderError
            On any upstream failure.
        """
        ...  # pragma: no cover — Protocol

    def delete_task(self, *, external_id: str) -> None:
        """Delete (or close, per provider semantics) the remote record.

        Some providers (GitHub Issues) cannot truly delete and instead
        close-as-not-planned; that's a provider-implementation detail.
        Callers treat this method as "make this record no longer present
        in :meth:`list_tasks` output".

        Raises
        ------
        anvil.sync.errors.SyncProviderError
            On any upstream failure other than the record already being absent.
        """
        ...  # pragma: no cover — Protocol

    def health_check(self) -> ProviderHealth:
        """Probe the provider for reachability + credential validity.

        MUST NOT raise: a fully-broken provider returns
        ``ProviderHealth(available=False, auth_configured=False, error=...)``
        rather than propagating the underlying error. The CLI's health
        screen relies on this so it can tabulate every provider's status
        in one pass without each unhealthy one aborting the run.
        """
        ...  # pragma: no cover — Protocol
