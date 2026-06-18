"""``httpx``-based REST transport for GitHub Issues (Phase 8 Wave 2).

Used as the fallback when ``gh`` is not installed or not authenticated.
Reads ``GITHUB_TOKEN`` from the environment; takes the repo target
explicitly (``<owner>/<repo>``) from the constructor.

Like the gh CLI client, this module is deliberately thin: it makes the
HTTP calls, classifies failures into the SyncProviderError hierarchy,
and returns parsed JSON dicts/lists. Business logic (status mapping,
ExternalTask construction, etc.) lives in the provider.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from anvil.sync.errors import (
    AuthenticationFailed,
    ProviderUnavailable,
    RateLimitExceeded,
    SyncProviderError,
)

__all__ = [
    "GITHUB_API_BASE",
    "GITHUB_API_VERSION",
    "GITHUB_HTTP_DEFAULT_TIMEOUT",
    "GithubHttpClient",
]


# Canonical GitHub REST API base. Hard-coded — anvil does not
# target GitHub Enterprise in v0. If/when GHE support arrives, take a
# ``api_base`` arg in the constructor and default to this.
GITHUB_API_BASE = "https://api.github.com"

# X-GitHub-Api-Version header value. Pinning to a specific version is
# the GitHub-recommended way to avoid silent breakage when GitHub rolls
# new defaults; updating is a deliberate one-line change here.
GITHUB_API_VERSION = "2022-11-28"

# Default per-request timeout in seconds. Matches the gh CLI client's
# timeout so behaviour is consistent across transports.
GITHUB_HTTP_DEFAULT_TIMEOUT = 30.0


def _classify_http_response(response: httpx.Response) -> SyncProviderError:
    """Map a non-2xx httpx response to the right SyncProviderError subclass.

    Inspects status code AND GitHub's rate-limit headers; a 403 with
    ``X-RateLimit-Remaining: 0`` is a rate-limit, not an auth failure
    (GitHub uses 403 for both, distinguished only by the header).
    """
    status = response.status_code
    # Truncate the body — error messages should be greppable, not novels.
    try:
        body_excerpt = response.text[:300]
    except Exception:
        body_excerpt = "<unreadable body>"

    if status == 401:
        return AuthenticationFailed(
            f"GitHub API rejected credentials (HTTP 401): {body_excerpt}"
        )
    if status == 403:
        # Rate limit signal lives in headers, not the body.
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining == "0":
            reset = response.headers.get("x-ratelimit-reset", "<unknown>")
            return RateLimitExceeded(
                f"GitHub primary rate limit exceeded (resets at unix={reset})"
            )
        # Secondary rate limits also come through as 403.
        if "secondary rate limit" in body_excerpt.lower():
            return RateLimitExceeded(
                f"GitHub secondary rate limit exceeded: {body_excerpt}"
            )
        return AuthenticationFailed(
            f"GitHub API forbidden (HTTP 403): {body_excerpt}"
        )
    if status == 429:
        return RateLimitExceeded(
            f"GitHub returned HTTP 429: {body_excerpt}"
        )
    if 500 <= status < 600:
        return ProviderUnavailable(
            f"GitHub API server error (HTTP {status}): {body_excerpt}"
        )
    # Catch-all for unexpected 4xx (422, 410, etc.) — caller-specific
    # handling of 422 (already-exists) happens at the provider layer
    # which inspects the raised error's status_code attribute.
    err = SyncProviderError(
        f"GitHub API error (HTTP {status}): {body_excerpt}"
    )
    # Attach status_code so the provider can branch on it without
    # re-parsing the message.
    err.status_code = status  # type: ignore[attr-defined]
    err.response_body = body_excerpt  # type: ignore[attr-defined]
    return err


class GithubHttpClient:
    """REST client for GitHub Issues via ``httpx``.

    Parameters
    ----------
    repo:
        ``<owner>/<repo>`` slug. Required; same rationale as
        :class:`anvil.sync.clients.gh_cli.GhCliClient`.
    token:
        GitHub PAT. If ``None`` (the default), reads ``GITHUB_TOKEN``
        from the environment at request time so a token rotation in the
        process env takes effect on the next call.
    timeout:
        Per-request timeout in seconds.
    base_url:
        API base URL. Defaults to :data:`GITHUB_API_BASE`; overridable
        for tests that point at a recorded mock URL.

    The HTTP client itself (``httpx.Client``) is lazily constructed on
    first call so import-time cost is zero and tests that never make
    requests don't open sockets.
    """

    __slots__ = ("repo", "_token", "timeout", "base_url", "_client")

    def __init__(
        self,
        *,
        repo: str,
        token: str | None = None,
        timeout: float = GITHUB_HTTP_DEFAULT_TIMEOUT,
        base_url: str = GITHUB_API_BASE,
    ) -> None:
        if not repo or "/" not in repo:
            raise ValueError(
                f"GithubHttpClient.repo must be '<owner>/<repo>', got {repo!r}"
            )
        self.repo = repo
        self._token = token
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------
    # Auth / client plumbing
    # ------------------------------------------------------------------

    def _resolve_token(self) -> str | None:
        """Return the token to use for this request.

        Explicit constructor token wins; otherwise read ``GITHUB_TOKEN``
        at call time (NOT init time) so rotation in the process env
        takes effect immediately.
        """
        if self._token is not None:
            return self._token
        return os.environ.get("GITHUB_TOKEN")

    def has_token(self) -> bool:
        """True iff a token is configured (constructor or env)."""
        return self._resolve_token() is not None

    def _headers(self) -> dict[str, str]:
        """Build the standard GitHub request headers."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "anvil-sync/1.8",
        }
        token = self._resolve_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    def close(self) -> None:
        """Close the underlying ``httpx.Client``. Safe to call repeatedly."""
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # Core invocation
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Issue one HTTP request; raise SyncProviderError on failure.

        Returns the raw :class:`httpx.Response` on 2xx; callers parse JSON
        themselves (some endpoints, like delete-labels, return 204 with
        no body, so we don't blanket-parse).
        """
        client = self._ensure_client()
        try:
            response = client.request(
                method,
                path,
                json=json_body,
                params=params,
                headers=self._headers(),
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(
                f"GitHub API timed out after {self.timeout}s on {method} {path}"
            ) from exc
        except httpx.ConnectError as exc:
            raise ProviderUnavailable(
                f"could not connect to GitHub API: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            # Catch-all for other httpx transport failures (proxy errors,
            # SSL errors, etc.) — all "the network is broken" semantically.
            raise ProviderUnavailable(
                f"GitHub API transport error: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise _classify_http_response(response)
        return response

    # ------------------------------------------------------------------
    # Issue CRUD
    # ------------------------------------------------------------------

    def create_issue(
        self,
        *,
        title: str,
        body: str,
        labels: list[str],
        assignees: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /repos/{repo}/issues; return the created issue payload."""
        payload: dict[str, Any] = {
            "title": title,
            "body": body,
            "labels": labels,
        }
        if assignees:
            payload["assignees"] = assignees
        response = self._request(
            "POST",
            f"/repos/{self.repo}/issues",
            json_body=payload,
        )
        data = response.json()
        if not isinstance(data, dict):
            raise SyncProviderError(
                f"GitHub returned non-dict JSON on create: {type(data).__name__}"
            )
        return data

    def update_issue(
        self,
        *,
        number: str,
        title: str | None = None,
        body: str | None = None,
        labels: list[str] | None = None,
        state: str | None = None,
        assignees: list[str] | None = None,
    ) -> dict[str, Any]:
        """PATCH /repos/{repo}/issues/{number}; return the updated payload.

        Unlike the gh CLI client, the REST API takes labels / state /
        assignees in a single PATCH body, so no second call needed.
        """
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if labels is not None:
            payload["labels"] = labels
        if state is not None:
            payload["state"] = state
        if assignees is not None:
            payload["assignees"] = assignees
        response = self._request(
            "PATCH",
            f"/repos/{self.repo}/issues/{number}",
            json_body=payload,
        )
        data = response.json()
        if not isinstance(data, dict):
            raise SyncProviderError(
                f"GitHub returned non-dict JSON on update: {type(data).__name__}"
            )
        return data

    def get_issue(self, *, number: str) -> dict[str, Any]:
        """GET /repos/{repo}/issues/{number}; raise on 404."""
        response = self._request(
            "GET",
            f"/repos/{self.repo}/issues/{number}",
        )
        data = response.json()
        if not isinstance(data, dict):
            raise SyncProviderError(
                f"GitHub returned non-dict JSON on get: {type(data).__name__}"
            )
        return data

    def get_issue_or_none(self, *, number: str) -> dict[str, Any] | None:
        """Like :meth:`get_issue` but returns ``None`` on HTTP 404."""
        try:
            return self.get_issue(number=number)
        except SyncProviderError as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code == 404:
                return None
            raise

    def list_issues(
        self,
        *,
        state: str = "all",
        per_page: int = 100,
        labels: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """GET /repos/{repo}/issues with full pagination.

        GitHub paginates via the ``Link`` header (RFC 5988). We follow
        ``rel="next"`` until it's absent. Default ``per_page=100`` is
        the API max — minimizes round-trips for large repos.
        """
        results: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "state": state,
            "per_page": per_page,
        }
        if labels:
            params["labels"] = ",".join(labels)
        path: str | None = f"/repos/{self.repo}/issues"
        # Track visited paths to break a malformed-Link-header infinite loop
        # (e.g. a buggy proxy that returns the same path as rel="next").
        # Hard cap of 1000 pages is far past any realistic repo size and
        # protects the polling loop from a never-terminating request train.
        visited: set[str] = set()
        max_pages = 1000
        while path is not None and len(visited) < max_pages:
            if path in visited:
                break
            visited.add(path)
            response = self._request("GET", path, params=params)
            page = response.json()
            if not isinstance(page, list):
                raise SyncProviderError(
                    f"GitHub returned non-list JSON on list: {type(page).__name__}"
                )
            # GitHub's /issues endpoint includes pull requests; filter them
            # out by the presence of a "pull_request" key (PRs have it,
            # plain issues don't).
            results.extend(item for item in page if "pull_request" not in item)
            # Parse Link header for next-page URL.
            link = response.headers.get("link") or response.headers.get("Link")
            path = _parse_next_link(link)
            # After the first request, the URL is absolute (from Link),
            # so params are already encoded in it — don't double-send.
            params = {}
        return results

    def close_issue(self, *, number: str) -> dict[str, Any]:
        """Close issue ``number`` via PATCH state=closed."""
        return self.update_issue(number=number, state="closed")


def _parse_next_link(link_header: str | None) -> str | None:
    """Extract the ``rel="next"`` URL from an RFC 5988 Link header.

    Returns the path portion (drops the host) so subsequent
    :func:`httpx.Client.request` calls reuse the existing base_url.
    Returns ``None`` if there is no next page.
    """
    if not link_header:
        return None
    # Header shape: <https://api.github.com/...?page=2>; rel="next", <...>; rel="last"
    for part in link_header.split(","):
        segment = part.strip()
        if 'rel="next"' not in segment:
            continue
        # The URL is enclosed in < >.
        start = segment.find("<")
        end = segment.find(">", start + 1)
        if start == -1 or end == -1:
            continue
        url = segment[start + 1 : end]
        # Strip the scheme + host so the client reuses base_url.
        for prefix in ("https://api.github.com", "http://api.github.com"):
            if url.startswith(prefix):
                return url[len(prefix) :]
        return url
    return None
