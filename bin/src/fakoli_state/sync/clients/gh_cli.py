"""``gh`` CLI transport for the GitHub Issues sync provider (Phase 8 Wave 2).

Wraps ``subprocess.run`` so the provider sees a clean Python surface
instead of argv lists and JSON parsing. Every call:

1. Builds an argv list — ``gh`` is the program; subcommand + flags follow.
2. Invokes via :func:`subprocess.run` with ``capture_output=True``,
   ``text=True``, and an explicit ``timeout`` (default 30 s).
3. On non-zero exit, classifies the failure and raises the matching
   :class:`fakoli_state.sync.errors.SyncProviderError` subclass.
4. On success, parses stdout as JSON (every command here is invoked with
   ``--json`` flags) and returns the parsed structure.

The functions here are deliberately thin — no business logic, no status
mapping, no model construction. That belongs in the provider; this layer
is purely "talk to gh and return JSON-or-raise".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from fakoli_state.sync.errors import (
    AuthenticationFailed,
    ProviderUnavailable,
    RateLimitExceeded,
    SyncProviderError,
)

__all__ = [
    "GH_DEFAULT_TIMEOUT",
    "GhCliClient",
    "GhCliResult",
]


# Default per-call timeout (seconds). 30 is generous for any single ``gh``
# command (issue create/edit/view are O(<1s)); set on every call so a
# hung gh process can't block the sync loop forever.
GH_DEFAULT_TIMEOUT = 30.0


# Regex used to extract the issue number from ``gh issue create``'s stdout.
# ``gh`` prints something like ``https://github.com/owner/repo/issues/42``
# on a line by itself, but it may print preamble lines (deprecation
# warnings, color escape resets) before the URL. Matching anywhere in
# any line is more robust than ``stdout.splitlines()[-1]``.
_ISSUE_URL_RE = re.compile(r"https?://\S+/issues/(\d+)/?\s*$")


class GhCliResult:
    """Parsed result of one successful ``gh`` invocation.

    Wraps the parsed JSON payload (``data``) plus the raw stdout / stderr
    strings so callers can surface debug info on parse failures.
    """

    __slots__ = ("data", "stdout", "stderr")

    def __init__(self, *, data: Any, stdout: str, stderr: str) -> None:
        self.data = data
        self.stdout = stdout
        self.stderr = stderr


def _classify_gh_failure(returncode: int, stderr: str) -> SyncProviderError:
    """Map a ``gh`` non-zero exit to the right SyncProviderError subclass.

    ``gh`` itself does not have stable exit-code semantics across error
    types; it almost always exits 1. The signal is in stderr text. We
    look for the canonical phrases ``gh`` emits for the failure modes
    the Protocol callers care about.
    """
    stderr_lower = stderr.lower()
    # Auth phrasing: ``gh`` says "authentication required" or
    # "Bad credentials" or "could not find any credentials".
    if (
        "authentication required" in stderr_lower
        or "bad credentials" in stderr_lower
        or "could not find any credentials" in stderr_lower
        or "you are not logged" in stderr_lower
        or "gh auth login" in stderr_lower
    ):
        return AuthenticationFailed(
            f"gh authentication failed (exit {returncode}): {stderr.strip()}"
        )
    # Rate-limit phrasing.
    if "rate limit" in stderr_lower or "api rate limit exceeded" in stderr_lower:
        return RateLimitExceeded(
            f"gh rate-limited (exit {returncode}): {stderr.strip()}"
        )
    # Network / unreachable phrasing.
    if (
        "could not resolve host" in stderr_lower
        or "connection refused" in stderr_lower
        or "network is unreachable" in stderr_lower
        or "timeout" in stderr_lower
    ):
        return ProviderUnavailable(
            f"gh transport unavailable (exit {returncode}): {stderr.strip()}"
        )
    # Default catch-all.
    return SyncProviderError(
        f"gh command failed (exit {returncode}): {stderr.strip()}"
    )


class GhCliClient:
    """Thin wrapper over ``subprocess.run`` for the ``gh`` CLI.

    Stateless except for the configured ``repo`` (``<owner>/<repo>``) the
    provider scopes operations to. All methods are safe to call repeatedly;
    each invocation spawns a fresh subprocess.

    Parameters
    ----------
    repo:
        ``<owner>/<repo>`` slug passed via ``--repo`` to every command.
        Required — the provider always knows which repo it is targeting,
        and leaving it out would silently fall back to ``gh``'s "current
        git remote" heuristic which is wrong in worktrees / detached dirs.
    timeout:
        Per-call timeout in seconds. Set on every :func:`subprocess.run`
        call so a hung process can't block the polling loop.
    """

    __slots__ = ("repo", "timeout")

    def __init__(self, *, repo: str, timeout: float = GH_DEFAULT_TIMEOUT) -> None:
        if not repo or "/" not in repo:
            raise ValueError(
                f"GhCliClient.repo must be '<owner>/<repo>', got {repo!r}"
            )
        self.repo = repo
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Core invocation
    # ------------------------------------------------------------------

    def _run(self, argv: list[str], *, parse_json: bool = True) -> GhCliResult:
        """Run ``gh <argv>`` and return the parsed result, or raise.

        ``argv`` is the list of args AFTER the leading ``gh`` program
        name (callers pass ``["issue", "view", "42", "--json", ...]``).
        Catches every failure mode the Protocol contract cares about.
        """
        full_argv = ["gh", *argv]
        # Force C locale so ``_classify_gh_failure``'s English-phrase
        # scan ("authentication required", "rate limit", "could not
        # resolve host") works regardless of the operator's LANG /
        # LC_ALL — localized ``gh`` builds would otherwise emit
        # translated messages and every stderr classification would
        # fall through to the catch-all SyncProviderError.
        env = os.environ.copy()
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
        try:
            completed = subprocess.run(
                full_argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            # gh not on PATH — distinct enough to deserve a specific
            # message so the user knows to install it / switch transport.
            raise ProviderUnavailable(
                "gh CLI not found on PATH; install from https://cli.github.com "
                "or set transport='http' and GITHUB_TOKEN"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderUnavailable(
                f"gh command timed out after {self.timeout}s: {' '.join(full_argv)}"
            ) from exc
        except OSError as exc:
            # Permission errors, ENOEXEC, etc. — wrap as transport failure.
            raise ProviderUnavailable(
                f"failed to invoke gh: {exc}"
            ) from exc

        if completed.returncode != 0:
            raise _classify_gh_failure(completed.returncode, completed.stderr or "")

        if not parse_json:
            return GhCliResult(
                data=None,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

        try:
            data = json.loads(completed.stdout) if completed.stdout.strip() else None
        except json.JSONDecodeError as exc:
            raise SyncProviderError(
                f"gh returned malformed JSON: {exc}; stdout[:200]="
                f"{completed.stdout[:200]!r}"
            ) from exc

        return GhCliResult(
            data=data,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    # ------------------------------------------------------------------
    # Health / auth probing
    # ------------------------------------------------------------------

    def version(self) -> str:
        """Return the ``gh --version`` first line, or raise ProviderUnavailable.

        Used by the provider's transport-selection logic at init time:
        if this raises, ``gh`` is not usable and we fall back to HTTP.
        """
        result = self._run(["--version"], parse_json=False)
        first_line = (result.stdout or "").splitlines()[0] if result.stdout else ""
        return first_line.strip()

    def auth_status(self) -> bool:
        """Return True iff ``gh auth status`` exits 0; False otherwise.

        Does NOT raise on auth failure (that's the literal signal we want
        to capture). Only raises if ``gh`` itself cannot be invoked.
        """
        env = os.environ.copy()
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
        try:
            completed = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise ProviderUnavailable(
                "gh CLI not found on PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderUnavailable(
                f"gh auth status timed out after {self.timeout}s"
            ) from exc
        return completed.returncode == 0

    # ------------------------------------------------------------------
    # Issue CRUD
    # ------------------------------------------------------------------

    # Fields requested for every issue read. Centralised so create / edit /
    # view / list all parse the same shape.
    ISSUE_VIEW_FIELDS = (
        "number,title,body,state,labels,assignees,url,updatedAt,id"
    )
    ISSUE_LIST_FIELDS = (
        "number,title,body,state,labels,assignees,url,updatedAt,id"
    )

    def create_issue(
        self,
        *,
        title: str,
        body: str,
        labels: list[str],
        assignees: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new issue; return the parsed view of the created record.

        ``gh issue create`` itself only prints the URL on success, so we
        immediately ``gh issue view <number> --json ...`` to get the full
        payload back in one shape that matches edit / view / list.
        """
        argv = [
            "issue",
            "create",
            "--repo",
            self.repo,
            "--title",
            title,
            "--body",
            body,
        ]
        for label in labels:
            argv.extend(["--label", label])
        if assignees:
            for assignee in assignees:
                argv.extend(["--assignee", assignee])
        create_result = self._run(argv, parse_json=False)
        # gh prints the issue URL on stdout; scan EVERY line for the
        # ``/issues/<n>`` pattern rather than assuming the URL is the
        # last line. Preamble (deprecation warnings, color resets) or
        # trailing blank lines would otherwise break ``[-1]``.
        stdout = create_result.stdout or ""
        issue_number: str | None = None
        for line in stdout.splitlines():
            m = _ISSUE_URL_RE.search(line.strip())
            if m:
                issue_number = m.group(1)
                break
        if issue_number is None:
            raise SyncProviderError(
                "gh issue create succeeded but stdout did not contain an "
                f"issue URL; got: {stdout!r}"
            )
        return self.view_issue(number=issue_number)

    def edit_issue(
        self,
        *,
        number: str,
        title: str | None = None,
        body: str | None = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Edit an existing issue; return the post-edit view payload.

        ``state`` accepts ``"open"`` or ``"closed"`` — we translate to
        ``gh issue close`` / ``gh issue reopen`` because ``gh issue edit``
        does not have a ``--state`` flag.
        """
        # Apply state transition first (if any). gh issue close/reopen
        # are separate commands.
        if state == "closed":
            self._run(
                [
                    "issue",
                    "close",
                    number,
                    "--repo",
                    self.repo,
                ],
                parse_json=False,
            )
        elif state == "open":
            self._run(
                [
                    "issue",
                    "reopen",
                    number,
                    "--repo",
                    self.repo,
                ],
                parse_json=False,
            )

        # Edit other fields if anything was requested.
        if title is not None or body is not None or add_labels or remove_labels:
            argv = [
                "issue",
                "edit",
                number,
                "--repo",
                self.repo,
            ]
            if title is not None:
                argv.extend(["--title", title])
            if body is not None:
                argv.extend(["--body", body])
            for label in add_labels or []:
                argv.extend(["--add-label", label])
            for label in remove_labels or []:
                argv.extend(["--remove-label", label])
            self._run(argv, parse_json=False)

        return self.view_issue(number=number)

    def view_issue(self, *, number: str) -> dict[str, Any]:
        """Return the full JSON view of issue ``number``.

        Raises :class:`SyncProviderError` on every non-zero ``gh`` exit;
        callers that want 404-tolerance use :meth:`view_issue_or_none`.
        """
        result = self._run(
            [
                "issue",
                "view",
                number,
                "--repo",
                self.repo,
                "--json",
                self.ISSUE_VIEW_FIELDS,
            ]
        )
        if not isinstance(result.data, dict):
            raise SyncProviderError(
                f"gh issue view returned non-dict JSON: {type(result.data).__name__}"
            )
        return result.data

    def view_issue_or_none(self, *, number: str) -> dict[str, Any] | None:
        """Same as :meth:`view_issue` but returns ``None`` on 404.

        ``gh issue view`` of a non-existent number prints a "could not
        find any issue" message and exits 1. Distinguishing that from
        every other failure lets the provider implement the Protocol's
        "fetch returns None on tombstone" contract.
        """
        try:
            return self.view_issue(number=number)
        except SyncProviderError as exc:
            msg = str(exc).lower()
            if (
                "could not find" in msg
                or "no issue" in msg
                or "not found" in msg
            ):
                return None
            raise

    def list_issues(
        self,
        *,
        state: str = "all",
        limit: int = 1000,
        labels: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """List issues in the configured repo.

        ``gh issue list --limit N`` handles pagination internally up to
        N records; we default to 1000 (well past any realistic plugin
        usage) so callers always get the full list in one call.
        """
        argv = [
            "issue",
            "list",
            "--repo",
            self.repo,
            "--state",
            state,
            "--limit",
            str(limit),
            "--json",
            self.ISSUE_LIST_FIELDS,
        ]
        for label in labels or []:
            argv.extend(["--label", label])
        result = self._run(argv)
        if not isinstance(result.data, list):
            raise SyncProviderError(
                f"gh issue list returned non-list JSON: {type(result.data).__name__}"
            )
        return result.data

    def close_issue(self, *, number: str) -> None:
        """Close issue ``number``. GitHub cannot truly delete an issue."""
        self._run(
            [
                "issue",
                "close",
                number,
                "--repo",
                self.repo,
            ],
            parse_json=False,
        )
