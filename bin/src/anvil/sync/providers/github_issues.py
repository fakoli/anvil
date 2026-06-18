"""GitHub Issues :class:`SyncProvider` implementation (Phase 8 Wave 2 Task 4).

First production sync provider. Dual transport:

- ``gh_cli`` (preferred when ``gh`` is installed and authenticated) —
  reuses the user's existing ``gh auth`` session; no PAT plumbing.
- ``http`` (fallback) — direct REST via ``httpx``; reads
  ``GITHUB_TOKEN`` from the environment.

The provider implements the :class:`anvil.sync.provider.SyncProvider`
Protocol fully:

- :meth:`push_task` — create-or-update based on whether a mapping exists.
- :meth:`fetch_task` — return current remote payload, or ``None`` on 404.
- :meth:`list_tasks` — full list of repo issues (paginated transparently).
- :meth:`delete_task` — closes the issue (GitHub cannot truly delete).
- :meth:`health_check` — non-throwing reachability + auth probe.

Auto-registers as ``"github_issues"`` in
:data:`anvil.sync.registry.PROVIDER_REGISTRY` at module load.
"""

from __future__ import annotations

import datetime
import os
import re
from typing import TYPE_CHECKING, Any, Literal

from anvil.state.models import TaskStatus
from anvil.sync.clients.gh_cli import GhCliClient
from anvil.sync.clients.github_http import GithubHttpClient
from anvil.sync.errors import (
    ProviderUnavailable,
    SyncProviderError,
)
from anvil.sync.provider import ExternalRef, ExternalTask, ProviderHealth
from anvil.sync.registry import register_sync_provider

if TYPE_CHECKING:
    from anvil.state.models import Task

__all__ = [
    "STATUS_TO_LABEL",
    "LABEL_TO_STATUS",
    "DONE_STATUSES",
    "GitHubIssuesProvider",
]


# ---------------------------------------------------------------------------
# Status ↔ label mapping — the central contract
# ---------------------------------------------------------------------------
#
# Every :class:`anvil.state.models.TaskStatus` enum value maps to
# exactly ONE GitHub label, prefixed ``status:`` so it sorts together in
# the GitHub UI and does not collide with user-defined labels.
#
# GitHub also has issue *state* (open/closed). ``done`` tasks become
# closed issues; everything else (including ``rejected``, which is a
# "won't fix" outcome) stays open. The label is the durable source of
# truth for *which* fakoli status the task is in; the open/closed bit
# is a UX nicety for GitHub's own search filters.
#
# Centralised here so push and fetch agree on the mapping verbatim.

STATUS_TO_LABEL: dict[TaskStatus, str] = {
    TaskStatus.proposed: "status:proposed",
    TaskStatus.drafted: "status:drafted",
    TaskStatus.reviewed: "status:reviewed",
    TaskStatus.ready: "status:ready",
    TaskStatus.claimed: "status:claimed",
    TaskStatus.in_progress: "status:in-progress",
    TaskStatus.blocked: "status:blocked",
    TaskStatus.needs_review: "status:needs-review",
    TaskStatus.accepted: "status:accepted",
    TaskStatus.done: "status:done",
    TaskStatus.rejected: "status:rejected",
}

# Reverse mapping. Built once at import; assertions in
# :class:`TestStatusLabelMapping` guarantee lossless round-trip.
LABEL_TO_STATUS: dict[str, TaskStatus] = {
    label: status for status, label in STATUS_TO_LABEL.items()
}

# Statuses that translate to a closed GitHub issue (rather than open).
# Only ``done`` qualifies — ``rejected`` stays open so a human looking at
# the repo can see it was actively rejected, not silently archived.
DONE_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.done})


def _status_to_state(status: TaskStatus) -> str:
    """Return ``"closed"`` for done statuses, ``"open"`` otherwise."""
    return "closed" if status in DONE_STATUSES else "open"


def _all_status_labels() -> set[str]:
    """Set of every label this provider manages (for filtering on update)."""
    return set(STATUS_TO_LABEL.values())


# ---------------------------------------------------------------------------
# Body footer — durable reverse link from GitHub issue back to local task
# ---------------------------------------------------------------------------


_FOOTER_TEMPLATE = "\n\n---\n_synced from anvil task {task_id}_"
_FOOTER_RE = re.compile(
    r"\n\n---\n_synced from anvil task ([A-Za-z0-9_\-]+)_\s*$"
)


def _compose_body(task_description: str, task_id: str) -> str:
    """Return the GitHub-issue body for ``task``: description + footer."""
    return task_description + _FOOTER_TEMPLATE.format(task_id=task_id)


def _strip_footer(body: str) -> str:
    """Strip the anvil footer if present (for round-trip fidelity).

    Used when constructing an :class:`ExternalTask` from a remote payload
    so callers see the body the agent originally wrote, not the body +
    footer that's actually stored on GitHub.
    """
    return _FOOTER_RE.sub("", body or "")


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


Transport = Literal["auto", "gh_cli", "http"]


class GitHubIssuesProvider:
    """Concrete :class:`anvil.sync.provider.SyncProvider` for GitHub Issues.

    Parameters
    ----------
    repo:
        ``<owner>/<repo>``. If ``None``, reads ``GITHUB_REPOSITORY`` from
        the environment (the same variable GitHub Actions sets). Required
        either way — without it we cannot scope any API call.
    transport:
        ``"auto"`` (default) probes ``gh --version`` + ``gh auth status``
        once at init and picks ``gh_cli`` if both succeed, ``http``
        otherwise. ``"gh_cli"`` and ``"http"`` force the respective
        transport regardless of availability. Cached for the instance
        lifetime so we don't re-probe on every call.
    token:
        Optional explicit token for the HTTP transport. Defaults to
        reading ``GITHUB_TOKEN`` at request time.
    """

    # Class-level Protocol attributes. Snake_case per Wave 1 critic SF-3.
    provider_id: str = "github_issues"
    display_name: str = "GitHub Issues"

    def __init__(
        self,
        *,
        repo: str | None = None,
        transport: Transport = "auto",
        token: str | None = None,
        gh_client: GhCliClient | None = None,
        http_client: GithubHttpClient | None = None,
    ) -> None:
        resolved_repo = repo or os.environ.get("GITHUB_REPOSITORY")
        if not resolved_repo:
            raise ValueError(
                "GitHubIssuesProvider needs repo='<owner>/<repo>' or "
                "GITHUB_REPOSITORY env var; got neither"
            )
        if "/" not in resolved_repo:
            raise ValueError(
                f"repo must be '<owner>/<repo>', got {resolved_repo!r}"
            )
        self.repo = resolved_repo
        self._token = token

        # Lazy: only build the client we actually pick. Tests can inject
        # pre-built clients via the gh_client / http_client kwargs.
        self._gh_client = gh_client
        self._http_client = http_client

        # Resolve transport ONCE. Cached for the lifetime of the
        # provider; callers that want to re-probe build a fresh instance.
        self._transport: Literal["gh_cli", "http"] = self._select_transport(transport)

    # ------------------------------------------------------------------
    # Transport selection
    # ------------------------------------------------------------------

    def _select_transport(
        self, requested: Transport
    ) -> Literal["gh_cli", "http"]:
        """Return the actually-used transport: ``gh_cli`` or ``http``.

        ``requested == "auto"``: try ``gh --version`` + ``gh auth status``;
        on either failure fall back to http. Explicit ``"gh_cli"`` or
        ``"http"`` bypasses the probe and returns as requested.
        """
        if requested == "gh_cli":
            return "gh_cli"
        if requested == "http":
            return "http"
        # auto: probe gh, fall back to http.
        try:
            probe = self._make_gh_client()
            probe.version()
            if probe.auth_status():
                return "gh_cli"
        except SyncProviderError:
            # gh missing / broken — fall through to http.
            pass
        return "http"

    def _make_gh_client(self) -> GhCliClient:
        if self._gh_client is None:
            self._gh_client = GhCliClient(repo=self.repo)
        return self._gh_client

    def _make_http_client(self) -> GithubHttpClient:
        if self._http_client is None:
            self._http_client = GithubHttpClient(repo=self.repo, token=self._token)
        return self._http_client

    def close(self) -> None:
        """Release any underlying transport handles. Safe to call repeatedly.

        Called from the CLI's ``_sync_provider_dispatch`` finally block so
        long-running ``--watch`` daemons don't leak the ``httpx.Client``
        connection pool. The ``gh`` CLI client is process-spawn per call
        (no persistent connection), so only the HTTP transport needs
        cleanup, but we guard both for forward-compatibility.
        """
        if self._http_client is not None:
            try:
                self._http_client.close()
            except Exception:  # noqa: BLE001 — close() must never raise
                # Closing twice or after a transport error must be silent;
                # the daemon is shutting down and there's nowhere to surface.
                pass

    # ------------------------------------------------------------------
    # Internal: payload → ExternalRef / ExternalTask
    # ------------------------------------------------------------------

    def _payload_to_external_ref(self, payload: dict[str, Any]) -> ExternalRef:
        """Build an :class:`ExternalRef` from a GitHub issue JSON payload.

        Both transports return the same shape for issue fields we care
        about: ``number`` (int), ``url`` / ``html_url`` (str).
        """
        number = payload.get("number")
        if number is None:
            raise SyncProviderError(
                f"GitHub issue payload missing 'number': keys={list(payload)}"
            )
        # gh CLI returns ``url`` (HTML URL); REST returns ``html_url``.
        url = payload.get("html_url") or payload.get("url")
        return ExternalRef(
            provider_id=self.provider_id,
            external_id=str(number),
            url=url,
        )

    def _payload_to_external_task(self, payload: dict[str, Any]) -> ExternalTask:
        """Build an :class:`ExternalTask` from a GitHub issue JSON payload.

        Normalises differences between the two transports:

        - ``labels`` is a list of dicts (REST) or list of dicts with
          ``name`` (gh CLI) — both expose ``name``.
        - ``assignees`` is a list of dicts with ``login`` in both.
        - ``updatedAt`` (gh CLI) vs ``updated_at`` (REST) — pick whichever
          is present.
        - ``state`` is the GitHub-native ``"open"`` / ``"closed"``; we
          surface this as :attr:`ExternalTask.status_label` and rely on
          the labels for the richer fakoli status (parsed by callers via
          :data:`LABEL_TO_STATUS`).
        """
        number = payload.get("number")
        if number is None:
            raise SyncProviderError(
                f"GitHub issue payload missing 'number': keys={list(payload)}"
            )

        labels_raw = payload.get("labels") or []
        labels = [
            (lbl.get("name") if isinstance(lbl, dict) else str(lbl))
            for lbl in labels_raw
        ]
        labels = [name for name in labels if name]

        assignees_raw = payload.get("assignees") or []
        assignees = [
            (a.get("login") if isinstance(a, dict) else str(a))
            for a in assignees_raw
        ]
        assignees = [login for login in assignees if login]

        updated_raw = (
            payload.get("updated_at")
            or payload.get("updatedAt")
        )
        if not updated_raw:
            raise SyncProviderError(
                "GitHub issue payload missing updated_at / updatedAt"
            )
        last_modified = _parse_github_datetime(updated_raw)

        body_raw = payload.get("body") or ""
        body = _strip_footer(body_raw)

        url = payload.get("html_url") or payload.get("url")

        return ExternalTask(
            external_id=str(number),
            title=payload.get("title") or "",
            body=body,
            status_label=payload.get("state"),
            url=url,
            last_modified=last_modified,
            provider_metadata={
                "labels": labels,
                "assignees": assignees,
                "issue_number": int(number),
                "issue_node_id": payload.get("id") or payload.get("node_id"),
            },
        )

    # ------------------------------------------------------------------
    # SyncProvider Protocol implementation
    # ------------------------------------------------------------------

    def push_task(
        self,
        *,
        task: Task,
        mapping: ExternalRef | None,
    ) -> ExternalRef:
        """Create or update the GitHub issue for ``task``.

        Branches on ``mapping``:

        - ``None`` → POST /repos/{repo}/issues (create).
        - non-None → PATCH /repos/{repo}/issues/{number} (update).

        On the create path, an HTTP 422 "already_exists" response is
        treated as "the remote already has this issue, fetch it and
        return its ExternalRef" rather than an error — that's the
        idempotency contract.
        """
        body = _compose_body(task.description, task.id)
        status_label = STATUS_TO_LABEL[task.status]
        state = _status_to_state(task.status)

        if self._transport == "gh_cli":
            client = self._make_gh_client()
            if mapping is None:
                try:
                    payload = client.create_issue(
                        title=task.title,
                        body=body,
                        labels=[status_label],
                    )
                except SyncProviderError as exc:
                    msg = str(exc).lower()
                    # Only trigger the O(N) title-search recovery when the
                    # error body explicitly says an issue with this title
                    # already exists. 422 can fire for other reasons
                    # (malformed label, invalid assignee, etc.) and we
                    # don't want to walk the full issue list every time.
                    if "already exists" in msg and "issue" in msg:
                        # gh prints "an issue with this title already exists"
                        # on stderr; mirror the http 422 already-exists
                        # contract by walking list_issues for a title match
                        # and returning that ref instead of propagating.
                        existing = self._find_issue_by_title(task.title)
                        if existing is not None:
                            return self._payload_to_external_ref(existing)
                    raise
                # Make sure issue state matches if status maps to closed.
                if state == "closed":
                    client.close_issue(number=str(payload["number"]))
                    payload = client.view_issue(number=str(payload["number"]))
                return self._payload_to_external_ref(payload)
            else:
                # Update path.
                # Remove all status:* labels we manage, then add the new one.
                existing_payload = client.view_issue(number=mapping.external_id)
                existing_labels = {
                    (lbl.get("name") if isinstance(lbl, dict) else str(lbl))
                    for lbl in existing_payload.get("labels") or []
                }
                managed_labels = _all_status_labels()
                to_remove = sorted(existing_labels & managed_labels - {status_label})
                payload = client.edit_issue(
                    number=mapping.external_id,
                    title=task.title,
                    body=body,
                    add_labels=[status_label],
                    remove_labels=to_remove,
                    state=state,
                )
                return self._payload_to_external_ref(payload)

        # http transport
        client_http = self._make_http_client()
        if mapping is None:
            try:
                payload = client_http.create_issue(
                    title=task.title,
                    body=body,
                    labels=[status_label],
                )
            except SyncProviderError as exc:
                # 422 already_exists: fetch the existing issue and return it.
                # Tighten the guard: 422 fires for other reasons too
                # (malformed label, invalid assignee, body too long), so
                # only trigger the O(N) title-search recovery when the
                # response body explicitly says ``already_exists`` AND
                # the resource is an Issue. This avoids a full
                # paginated walk on every unrelated 422.
                status_code = getattr(exc, "status_code", None)
                body_excerpt = (getattr(exc, "response_body", "") or "").lower()
                if (
                    status_code == 422
                    and "already_exists" in body_excerpt
                    and '"issue"' in body_excerpt
                ):
                    # Best-effort: search by title to find the existing issue.
                    existing = self._find_issue_by_title(task.title)
                    if existing is not None:
                        return self._payload_to_external_ref(existing)
                raise
            # Make sure issue state matches if status maps to closed.
            if state == "closed":
                payload = client_http.update_issue(
                    number=str(payload["number"]),
                    state="closed",
                )
            return self._payload_to_external_ref(payload)
        else:
            # PATCH /repos/{repo}/issues/{n} with labels=[...] REPLACES the
            # entire labels array on the GitHub side. We MUST fetch the
            # existing labels first and keep every non-status:* label
            # (user-added bug, area/*, priority:*, etc.) — otherwise a
            # routine status push would nuke them. The gh-cli update path
            # uses --add-label / --remove-label which is naturally
            # preserving; this is the equivalent for the REST transport.
            existing_payload = client_http.get_issue(number=mapping.external_id)
            existing_labels_raw = existing_payload.get("labels") or []
            existing_labels = [
                (lbl.get("name") if isinstance(lbl, dict) else str(lbl))
                for lbl in existing_labels_raw
            ]
            existing_labels = [name for name in existing_labels if name]
            managed = _all_status_labels()
            preserved = [name for name in existing_labels if name not in managed]
            new_labels = preserved + [status_label]
            payload = client_http.update_issue(
                number=mapping.external_id,
                title=task.title,
                body=body,
                labels=new_labels,
                state=state,
            )
            return self._payload_to_external_ref(payload)

    def _find_issue_by_title(self, title: str) -> dict[str, Any] | None:
        """Best-effort lookup of an existing issue by title.

        Used only on the 422 already-exists fallback path. Walks the
        full issue list looking for an exact title match. Returns the
        first match, or ``None`` if none found.

        Both sides are ``.strip()``'d because GitHub may store titles
        with trailing/leading whitespace (paste artifacts, line wrapping
        through ``gh issue create``'s argv plumbing). Without the strip,
        the duplicate-title recovery path silently fails on every issue
        the user actually pasted from a markdown editor.
        """
        try:
            if self._transport == "gh_cli":
                items = self._make_gh_client().list_issues(state="all")
            else:
                items = self._make_http_client().list_issues(state="all")
        except SyncProviderError:
            return None
        needle = title.strip()
        for item in items:
            if (item.get("title") or "").strip() == needle:
                return item
        return None

    def fetch_task(self, *, external_id: str) -> ExternalTask | None:
        """Return the current remote payload, or ``None`` on 404.

        The ``*_or_none`` client methods already return ``None`` on 404 per
        their name; any other ``SyncProviderError`` (auth, rate-limit,
        transport) propagates unchanged.
        """
        if self._transport == "gh_cli":
            payload = self._make_gh_client().view_issue_or_none(
                number=external_id
            )
        else:
            payload = self._make_http_client().get_issue_or_none(
                number=external_id
            )
        if payload is None:
            return None
        return self._payload_to_external_task(payload)

    def list_tasks(self) -> list[ExternalTask]:
        """Return every issue in the configured repo as an ExternalTask."""
        if self._transport == "gh_cli":
            payloads = self._make_gh_client().list_issues(state="all")
        else:
            payloads = self._make_http_client().list_issues(state="all")
        return [self._payload_to_external_task(p) for p in payloads]

    def delete_task(self, *, external_id: str) -> None:
        """Close the issue. GitHub cannot truly delete; closing is the
        contract-equivalent ("no longer in :meth:`list_tasks` of open
        issues"). 404 is treated as idempotent success.
        """
        try:
            if self._transport == "gh_cli":
                self._make_gh_client().close_issue(number=external_id)
            else:
                self._make_http_client().close_issue(number=external_id)
        except SyncProviderError as exc:
            # Idempotent: already gone is fine.
            status_code = getattr(exc, "status_code", None)
            msg = str(exc).lower()
            if status_code == 404 or "not found" in msg or "could not find" in msg:
                return
            raise

    def health_check(self) -> ProviderHealth:
        """Probe reachability + auth. MUST NOT raise per Protocol contract."""
        now = datetime.datetime.now(datetime.UTC)
        try:
            if self._transport == "gh_cli":
                client = self._make_gh_client()
                try:
                    client.version()
                except SyncProviderError as exc:
                    return ProviderHealth(
                        available=False,
                        auth_configured=False,
                        last_check_at=now,
                        error=f"gh CLI unavailable: {exc}",
                    )
                try:
                    authed = client.auth_status()
                except ProviderUnavailable as exc:
                    # ``gh auth status`` itself failed to run (timeout,
                    # ``gh`` vanished from PATH between probes, etc.).
                    # That's a transport-level miss, not a misconfig — make
                    # the operator-facing message say so explicitly rather
                    # than dropping into the catch-all "unexpected error".
                    return ProviderHealth(
                        available=False,
                        auth_configured=False,
                        last_check_at=now,
                        error=f"gh auth probe failed: {exc}",
                    )
                if not authed:
                    return ProviderHealth(
                        available=True,
                        auth_configured=False,
                        last_check_at=now,
                        error="gh auth status reports not authenticated; "
                        "run `gh auth login`",
                    )
                return ProviderHealth(
                    available=True,
                    auth_configured=True,
                    last_check_at=now,
                    error=None,
                )
            # http transport
            http = self._make_http_client()
            if not http.has_token():
                return ProviderHealth(
                    available=True,
                    auth_configured=False,
                    last_check_at=now,
                    error=(
                        "GITHUB_TOKEN env var not set; set it to a PAT "
                        "with 'repo' scope or install the gh CLI"
                    ),
                )
            # Token present: assume auth-ok without burning an API call.
            # A future iteration may add a GET /user probe behind a flag.
            return ProviderHealth(
                available=True,
                auth_configured=True,
                last_check_at=now,
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 — Protocol forbids raising
            # Belt-and-suspenders: any unexpected exception becomes a
            # health-fail rather than propagating. The Protocol docstring
            # is explicit that this method MUST NOT raise.
            return ProviderHealth(
                available=False,
                auth_configured=False,
                last_check_at=now,
                error=f"unexpected error during health check: {exc}",
            )


# ---------------------------------------------------------------------------
# Datetime parsing
# ---------------------------------------------------------------------------


def _parse_github_datetime(s: str) -> datetime.datetime:
    """Parse a GitHub-shaped ISO 8601 timestamp into a tz-aware UTC datetime.

    GitHub uses the ``Z`` suffix (RFC 3339) which Python's ``fromisoformat``
    accepts in 3.11+. We normalise to UTC explicitly so a future schema
    change that ships ``+00:00`` instead of ``Z`` still lands tz-aware UTC.
    """
    try:
        # Python 3.11+: fromisoformat handles ``Z`` natively.
        dt = datetime.datetime.fromisoformat(s)
    except (ValueError, TypeError) as exc:
        raise SyncProviderError(
            f"GitHub returned malformed timestamp {s!r}: {exc}"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    else:
        dt = dt.astimezone(datetime.UTC)
    return dt


# ---------------------------------------------------------------------------
# Auto-registration — must be at module bottom so the class definition
# above is already bound by the time the registry inspects it.
# ---------------------------------------------------------------------------

register_sync_provider(GitHubIssuesProvider.provider_id, GitHubIssuesProvider)
