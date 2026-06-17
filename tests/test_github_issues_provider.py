"""Tests for fakoli_state.sync.providers.github_issues — Phase 8 Wave 2 Task 4.

Every test mocks the transport layer (subprocess for gh CLI, ``responses``
library for HTTP) — NO live GitHub API calls. Ever.

Test layout
-----------
- TestProviderRegistration — auto-registration + snake_case provider_id.
- TestTransportSelection — auto/gh_cli/http branching.
- TestPushTaskGhCli — happy create, happy update, errors, idempotency.
- TestPushTaskHttp — same matrix via httpx mocked with ``responses``.
- TestFetchTask — happy path + 404 None + parse errors.
- TestListTasks — pagination + filtering + empty.
- TestDeleteTask — close + 404 idempotency.
- TestHealthCheck — gh ok/missing/unauth + http with/without token.
- TestStatusLabelMapping — every TaskStatus enum value covered, round-trip.
- TestProviderMetadata — populated on push, round-tripped on fetch.

All tests live in this single file because the suite layout convention is
one file per source module.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx  # httpx-native mocker (responses lib only mocks `requests`)

from fakoli_state.state.models import Task, TaskPriority, TaskStatus
from fakoli_state.sync import (
    PROVIDER_REGISTRY,
    AuthenticationFailed,
    ExternalRef,
    ExternalTask,
    ProviderUnavailable,
    RateLimitExceeded,
    SyncProviderError,
    get_sync_provider,
    list_sync_providers,
)
from fakoli_state.sync.clients.gh_cli import GhCliClient
from fakoli_state.sync.clients.github_http import GithubHttpClient
from fakoli_state.sync.providers.github_issues import (
    DONE_STATUSES,
    LABEL_TO_STATUS,
    STATUS_TO_LABEL,
    GitHubIssuesProvider,
    _compose_body,
    _parse_github_datetime,
    _strip_footer,
)

UTC = datetime.UTC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime.datetime:
    return datetime.datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


def _make_task(
    *,
    task_id: str = "T001",
    title: str = "Sample task",
    description: str = "Task body",
    status: TaskStatus = TaskStatus.in_progress,
) -> Task:
    return Task(
        id=task_id,
        feature_id="F001",
        title=title,
        description=description,
        status=status,
        priority=TaskPriority.medium,
        created_at=_now(),
        updated_at=_now(),
    )


def _make_gh_issue_payload(
    *,
    number: int = 42,
    title: str = "Sample task",
    body: str = "Task body\n\n---\n_synced from fakoli-state task T001_",
    state: str = "open",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    updated_at: str = "2026-05-25T12:00:00Z",
) -> dict[str, Any]:
    """Build a GitHub issue payload dict shaped like the REST API returns."""
    return {
        "number": number,
        "id": f"node_{number}",
        "title": title,
        "body": body,
        "state": state,
        "labels": [{"name": label} for label in (labels or ["status:in-progress"])],
        "assignees": [{"login": login} for login in (assignees or [])],
        "html_url": f"https://github.com/octo/repo/issues/{number}",
        "url": f"https://api.github.com/repos/octo/repo/issues/{number}",
        "updated_at": updated_at,
        "updatedAt": updated_at,
    }


class _FakeCompleted:
    """Lightweight stand-in for ``subprocess.CompletedProcess``."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip GITHUB_TOKEN / GITHUB_REPOSITORY by default; tests opt in."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)


@pytest.fixture
def gh_provider() -> GitHubIssuesProvider:
    """Provider with explicit gh_cli transport (no subprocess probe)."""
    return GitHubIssuesProvider(repo="octo/repo", transport="gh_cli")


@pytest.fixture
def http_provider() -> GitHubIssuesProvider:
    """Provider with explicit http transport + token configured."""
    return GitHubIssuesProvider(
        repo="octo/repo",
        transport="http",
        token="ghp_test_token",
    )


# ===========================================================================
# Registration
# ===========================================================================


class TestProviderRegistration:
    def test_auto_registers_on_sync_package_import(self) -> None:
        # The fakoli_state.sync import at the top of this module already
        # triggered registration via the providers package side-effect.
        assert "github_issues" in list_sync_providers()

    def test_provider_id_is_snake_case(self) -> None:
        assert GitHubIssuesProvider.provider_id == "github_issues"
        assert "-" not in GitHubIssuesProvider.provider_id

    def test_display_name_human_readable(self) -> None:
        assert GitHubIssuesProvider.display_name == "GitHub Issues"

    def test_get_sync_provider_returns_class(self) -> None:
        cls = get_sync_provider("github_issues")
        assert cls is GitHubIssuesProvider

    def test_registry_contains_provider_class_not_instance(self) -> None:
        assert PROVIDER_REGISTRY["github_issues"] is GitHubIssuesProvider


# ===========================================================================
# Transport selection
# ===========================================================================


class TestTransportSelection:
    def test_auto_picks_gh_when_available_and_authed(self, monkeypatch) -> None:
        # subprocess.run returns success for both --version and auth status.
        def fake_run(argv, **kwargs):  # noqa: ARG001
            return _FakeCompleted(returncode=0, stdout="gh version 2.0.0\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        provider = GitHubIssuesProvider(repo="octo/repo", transport="auto")
        assert provider._transport == "gh_cli"

    def test_auto_falls_back_to_http_when_gh_missing(self, monkeypatch) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            raise FileNotFoundError("gh not on PATH")

        monkeypatch.setattr(subprocess, "run", fake_run)
        provider = GitHubIssuesProvider(repo="octo/repo", transport="auto")
        assert provider._transport == "http"

    def test_auto_falls_back_to_http_when_gh_unauthed(self, monkeypatch) -> None:
        calls = []

        def fake_run(argv, **kwargs):  # noqa: ARG001
            calls.append(argv)
            if "--version" in argv:
                return _FakeCompleted(returncode=0, stdout="gh version 2.0.0\n")
            if "auth" in argv and "status" in argv:
                return _FakeCompleted(
                    returncode=1, stderr="You are not logged in"
                )
            return _FakeCompleted(returncode=0, stdout="{}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        provider = GitHubIssuesProvider(repo="octo/repo", transport="auto")
        assert provider._transport == "http"

    def test_explicit_gh_cli_skips_probe(self, monkeypatch) -> None:
        # If we forced gh_cli the constructor must NOT probe — verify by
        # raising on any subprocess call and checking init still succeeds.
        def fake_run(argv, **kwargs):  # noqa: ARG001
            raise AssertionError(f"transport='gh_cli' should not probe: {argv}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        provider = GitHubIssuesProvider(repo="octo/repo", transport="gh_cli")
        assert provider._transport == "gh_cli"

    def test_explicit_http_skips_probe(self, monkeypatch) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            raise AssertionError(f"transport='http' should not probe: {argv}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        provider = GitHubIssuesProvider(repo="octo/repo", transport="http")
        assert provider._transport == "http"

    def test_transport_is_cached_for_instance_lifetime(self, monkeypatch) -> None:
        probe_count = 0

        def fake_run(argv, **kwargs):  # noqa: ARG001
            nonlocal probe_count
            probe_count += 1
            return _FakeCompleted(returncode=0, stdout="ok\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        provider = GitHubIssuesProvider(repo="octo/repo", transport="auto")
        # After init, probe is done — push/fetch must not re-probe.
        baseline = probe_count
        assert provider._transport == "gh_cli"
        assert probe_count == baseline

    def test_repo_required(self) -> None:
        with pytest.raises(ValueError, match="repo"):
            GitHubIssuesProvider()

    def test_repo_must_have_slash(self) -> None:
        with pytest.raises(ValueError, match="<owner>/<repo>"):
            GitHubIssuesProvider(repo="bad", transport="http")

    def test_repo_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY", "octo/from-env")
        provider = GitHubIssuesProvider(transport="http")
        assert provider.repo == "octo/from-env"


# ===========================================================================
# push_task — gh CLI transport
# ===========================================================================


class TestPushTaskGhCli:
    def test_create_happy_path(self, monkeypatch, gh_provider) -> None:
        calls: list[list[str]] = []
        view_payload = _make_gh_issue_payload(number=99)

        def fake_run(argv, **kwargs):  # noqa: ARG001
            calls.append(argv)
            if "create" in argv:
                return _FakeCompleted(
                    returncode=0,
                    stdout="https://github.com/octo/repo/issues/99\n",
                )
            if "view" in argv:
                return _FakeCompleted(
                    returncode=0, stdout=json.dumps(view_payload)
                )
            return _FakeCompleted(returncode=0, stdout="{}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        task = _make_task(status=TaskStatus.in_progress)
        ref = gh_provider.push_task(task=task, mapping=None)
        assert isinstance(ref, ExternalRef)
        assert ref.external_id == "99"
        assert ref.provider_id == "github_issues"
        assert ref.url == "https://github.com/octo/repo/issues/99"
        # Verify a create command was issued.
        create_argv = next(a for a in calls if "create" in a)
        assert "--label" in create_argv
        assert "status:in-progress" in create_argv

    def test_update_happy_path(self, monkeypatch, gh_provider) -> None:
        existing_payload = _make_gh_issue_payload(
            number=42,
            labels=["status:ready", "bug"],
        )
        updated_payload = _make_gh_issue_payload(
            number=42,
            labels=["status:in-progress", "bug"],
        )
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):  # noqa: ARG001
            calls.append(argv)
            if "view" in argv and "42" in argv:
                # Return existing on first view, updated on second.
                payload = (
                    existing_payload
                    if calls.count(argv) == 1
                    else updated_payload
                )
                return _FakeCompleted(returncode=0, stdout=json.dumps(payload))
            return _FakeCompleted(returncode=0, stdout="ok\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        task = _make_task(status=TaskStatus.in_progress)
        existing_ref = ExternalRef(
            provider_id="github_issues",
            external_id="42",
            url="https://github.com/octo/repo/issues/42",
        )
        ref = gh_provider.push_task(task=task, mapping=existing_ref)
        assert ref.external_id == "42"
        # Verify edit was called with the new label and removed status:ready.
        edit_calls = [a for a in calls if "edit" in a]
        assert edit_calls, "expected an edit call"
        edit_argv = edit_calls[0]
        assert "--add-label" in edit_argv
        assert "status:in-progress" in edit_argv
        assert "--remove-label" in edit_argv
        assert "status:ready" in edit_argv

    def test_gh_not_installed_raises_provider_unavailable(
        self, monkeypatch, gh_provider
    ) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            raise FileNotFoundError("gh: command not found")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(ProviderUnavailable, match="gh CLI not found"):
            gh_provider.push_task(task=_make_task(), mapping=None)

    def test_gh_not_authed_raises_authentication_failed(
        self, monkeypatch, gh_provider
    ) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            return _FakeCompleted(
                returncode=1,
                stderr="authentication required: run gh auth login",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(AuthenticationFailed, match="gh authentication failed"):
            gh_provider.push_task(task=_make_task(), mapping=None)

    def test_subprocess_timeout_raises_provider_unavailable(
        self, monkeypatch, gh_provider
    ) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(ProviderUnavailable, match="timed out"):
            gh_provider.push_task(task=_make_task(), mapping=None)

    def test_done_status_closes_issue_on_create(
        self, monkeypatch, gh_provider
    ) -> None:
        calls: list[list[str]] = []
        view_payload = _make_gh_issue_payload(
            number=7, state="closed", labels=["status:done"]
        )

        def fake_run(argv, **kwargs):  # noqa: ARG001
            calls.append(argv)
            if "create" in argv:
                return _FakeCompleted(
                    returncode=0,
                    stdout="https://github.com/octo/repo/issues/7\n",
                )
            return _FakeCompleted(
                returncode=0, stdout=json.dumps(view_payload)
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        ref = gh_provider.push_task(
            task=_make_task(status=TaskStatus.done), mapping=None
        )
        assert ref.external_id == "7"
        # close must have been called after create.
        close_calls = [a for a in calls if "close" in a]
        assert close_calls

    def test_create_already_exists_returns_fetched_ref(
        self, monkeypatch, gh_provider
    ) -> None:
        """Regression mirroring the HTTP 422 already-exists test for gh-cli.

        ``gh issue create`` surfaces "already exists" failures via stderr.
        The provider must recover by listing repo issues and returning the
        matching ref, NOT propagate the SyncProviderError.
        """
        existing_payload = _make_gh_issue_payload(
            number=88, title="Sample task"
        )

        def fake_run(argv, **kwargs):  # noqa: ARG001
            if "create" in argv:
                return _FakeCompleted(
                    returncode=1,
                    stderr="an issue with this title already exists",
                )
            if "list" in argv:
                return _FakeCompleted(
                    returncode=0,
                    stdout=json.dumps([existing_payload]),
                )
            # view_issue called by _find_issue_by_title fallback shouldn't
            # actually fire here, but be defensive.
            return _FakeCompleted(
                returncode=0, stdout=json.dumps(existing_payload)
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        ref = gh_provider.push_task(
            task=_make_task(title="Sample task"), mapping=None
        )
        assert ref.external_id == "88"
        assert ref.provider_id == "github_issues"


# ===========================================================================
# push_task — HTTP transport
# ===========================================================================


class TestPushTaskHttp:
    def test_create_happy_path(self, http_provider) -> None:
        payload = _make_gh_issue_payload(number=101)
        with respx.mock(base_url="https://api.github.com") as mock:
            route = mock.post("/repos/octo/repo/issues").mock(
                return_value=httpx.Response(201, json=payload)
            )
            task = _make_task(status=TaskStatus.ready)
            ref = http_provider.push_task(task=task, mapping=None)
            assert ref.external_id == "101"
            assert ref.url == "https://github.com/octo/repo/issues/101"
            # Verify auth header was sent.
            assert route.called
            sent = route.calls[0].request
            assert sent.headers.get("Authorization") == "Bearer ghp_test_token"
            body = json.loads(sent.content)
            assert body["title"] == "Sample task"
            assert body["labels"] == ["status:ready"]

    def test_update_happy_path(self, http_provider) -> None:
        # The update path GETs first to preserve user-added labels (SF-3),
        # then PATCHes with the merged label list.
        existing = _make_gh_issue_payload(
            number=42, labels=["status:ready"]
        )
        updated = _make_gh_issue_payload(
            number=42, labels=["status:in-progress"]
        )
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues/42").mock(
                return_value=httpx.Response(200, json=existing)
            )
            route = mock.patch("/repos/octo/repo/issues/42").mock(
                return_value=httpx.Response(200, json=updated)
            )
            existing_ref = ExternalRef(
                provider_id="github_issues",
                external_id="42",
                url="https://github.com/octo/repo/issues/42",
            )
            ref = http_provider.push_task(
                task=_make_task(status=TaskStatus.in_progress),
                mapping=existing_ref,
            )
            assert ref.external_id == "42"
            body = json.loads(route.calls[0].request.content)
            assert body["labels"] == ["status:in-progress"]
            assert body["state"] == "open"

    def test_401_raises_authentication_failed(self, http_provider) -> None:
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.post("/repos/octo/repo/issues").mock(
                return_value=httpx.Response(
                    401, json={"message": "Bad credentials"}
                )
            )
            with pytest.raises(AuthenticationFailed, match="rejected credentials"):
                http_provider.push_task(task=_make_task(), mapping=None)

    def test_403_with_rate_limit_header_raises_rate_limit_exceeded(
        self, http_provider
    ) -> None:
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.post("/repos/octo/repo/issues").mock(
                return_value=httpx.Response(
                    403,
                    json={"message": "rate limit"},
                    headers={
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "1750000000",
                    },
                )
            )
            with pytest.raises(RateLimitExceeded, match="rate limit"):
                http_provider.push_task(task=_make_task(), mapping=None)

    def test_403_without_rate_limit_raises_authentication_failed(
        self, http_provider
    ) -> None:
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.post("/repos/octo/repo/issues").mock(
                return_value=httpx.Response(
                    403,
                    json={"message": "Resource not accessible"},
                    headers={"X-RateLimit-Remaining": "4999"},
                )
            )
            with pytest.raises(AuthenticationFailed, match="forbidden"):
                http_provider.push_task(task=_make_task(), mapping=None)

    def test_422_already_exists_returns_fetched_ref(self, http_provider) -> None:
        existing_payload = _make_gh_issue_payload(
            number=55, title="Sample task"
        )
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.post("/repos/octo/repo/issues").mock(
                return_value=httpx.Response(
                    422,
                    json={
                        "message": "Validation Failed",
                        "errors": [
                            {
                                # Real GitHub Issues 422 payload includes
                                # resource="Issue"; the provider's 422
                                # guard requires both already_exists AND
                                # the Issue resource so other 422s
                                # (malformed label, invalid assignee)
                                # don't trigger the O(N) title walk.
                                "resource": "Issue",
                                "code": "already_exists",
                                "field": "title",
                            }
                        ],
                    },
                )
            )
            # The fallback path walks list_issues looking for a title match.
            mock.get("/repos/octo/repo/issues").mock(
                return_value=httpx.Response(200, json=[existing_payload])
            )
            ref = http_provider.push_task(
                task=_make_task(title="Sample task"), mapping=None
            )
            assert ref.external_id == "55"

    def test_422_already_exists_without_issue_resource_does_not_walk(
        self, http_provider,
    ) -> None:
        """SF-10 regression — a 422 that says ``already_exists`` but is for
        a non-Issue resource (e.g. label, milestone) must NOT trigger the
        O(N) issue-list walk; the original SyncProviderError propagates.
        """
        # ``assert_all_called=False`` because the SF-10 guard correctly
        # prevents the list-issues route from being called — that's the
        # invariant under test.
        with respx.mock(
            base_url="https://api.github.com", assert_all_called=False,
        ) as mock:
            mock.post("/repos/octo/repo/issues").mock(
                return_value=httpx.Response(
                    422,
                    json={
                        "message": "Validation Failed",
                        "errors": [
                            {
                                "resource": "Label",
                                "code": "already_exists",
                                "field": "name",
                            }
                        ],
                    },
                )
            )
            # If the guard wrongly fires, this would be the call we'd see.
            list_route = mock.get("/repos/octo/repo/issues").mock(
                return_value=httpx.Response(200, json=[])
            )
            with pytest.raises(SyncProviderError):
                http_provider.push_task(
                    task=_make_task(title="Sample task"), mapping=None
                )
            assert list_route.called is False, (
                "SF-10 regression: 422 with non-Issue resource triggered "
                "the title-walk fallback"
            )

    def test_network_failure_raises_provider_unavailable(
        self, http_provider
    ) -> None:
        # respx in mock mode raises an unmatched-route exception which httpx
        # surfaces as a TransportError. Force a real connect error by
        # pointing at a deliberately-unreachable port.
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.post("/repos/octo/repo/issues").mock(
                side_effect=httpx.ConnectError("network down")
            )
            with pytest.raises(ProviderUnavailable):
                http_provider.push_task(task=_make_task(), mapping=None)

    def test_update_preserves_user_added_labels(self, http_provider) -> None:
        """Regression: PATCH /issues/{n} with labels=[...] REPLACES the labels
        array. Users frequently add their own labels (bug, priority:p1,
        area/frontend) by hand in the GitHub UI; a sync must not nuke those.

        Mirrors the gh-cli update path which correctly uses --add-label /
        --remove-label to preserve non-status labels.
        """
        # Existing issue carries a fakoli-managed status label PLUS two
        # user-curated labels that fakoli must never touch.
        existing_payload = _make_gh_issue_payload(
            number=42,
            labels=["status:ready", "bug", "priority:p1"],
        )
        updated_payload = _make_gh_issue_payload(
            number=42,
            labels=["bug", "priority:p1", "status:in-progress"],
        )
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues/42").mock(
                return_value=httpx.Response(200, json=existing_payload)
            )
            patch_route = mock.patch("/repos/octo/repo/issues/42").mock(
                return_value=httpx.Response(200, json=updated_payload)
            )
            existing_ref = ExternalRef(
                provider_id="github_issues",
                external_id="42",
                url="https://github.com/octo/repo/issues/42",
            )
            ref = http_provider.push_task(
                task=_make_task(status=TaskStatus.in_progress),
                mapping=existing_ref,
            )
        assert ref.external_id == "42"
        # The PATCH must carry the user labels alongside the new status:
        # ordering is preserved-first, then the new status label appended.
        body = json.loads(patch_route.calls[0].request.content)
        assert body["labels"] == ["bug", "priority:p1", "status:in-progress"]
        assert body["state"] == "open"


# ===========================================================================
# fetch_task
# ===========================================================================


class TestFetchTask:
    def test_happy_path_http(self, http_provider) -> None:
        payload = _make_gh_issue_payload(
            number=12,
            body="My description\n\n---\n_synced from fakoli-state task T001_",
            labels=["status:ready", "p1"],
            assignees=["octocat"],
        )
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues/12").mock(
                return_value=httpx.Response(200, json=payload)
            )
            task = http_provider.fetch_task(external_id="12")
            assert task is not None
            assert task.external_id == "12"
            assert task.title == "Sample task"
            # Footer must be stripped from the body.
            assert task.body == "My description"
            assert task.status_label == "open"
            assert task.provider_metadata["labels"] == ["status:ready", "p1"]
            assert task.provider_metadata["assignees"] == ["octocat"]
            assert task.provider_metadata["issue_number"] == 12
            # last_modified must be tz-aware UTC.
            assert task.last_modified.tzinfo is not None

    def test_404_returns_none(self, http_provider) -> None:
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues/9999").mock(
                return_value=httpx.Response(404, json={"message": "Not Found"})
            )
            assert http_provider.fetch_task(external_id="9999") is None

    def test_malformed_timestamp_raises_sync_provider_error(
        self, http_provider
    ) -> None:
        payload = _make_gh_issue_payload(number=1)
        payload["updated_at"] = "not-a-date"
        payload["updatedAt"] = "not-a-date"
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues/1").mock(
                return_value=httpx.Response(200, json=payload)
            )
            with pytest.raises(SyncProviderError, match="malformed timestamp"):
                http_provider.fetch_task(external_id="1")

    def test_gh_cli_happy_path(self, monkeypatch, gh_provider) -> None:
        payload = _make_gh_issue_payload(number=12, labels=["status:ready"])

        def fake_run(argv, **kwargs):  # noqa: ARG001
            return _FakeCompleted(returncode=0, stdout=json.dumps(payload))

        monkeypatch.setattr(subprocess, "run", fake_run)
        task = gh_provider.fetch_task(external_id="12")
        assert task is not None
        assert task.external_id == "12"
        assert task.provider_metadata["labels"] == ["status:ready"]

    def test_gh_cli_404_returns_none(self, monkeypatch, gh_provider) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            return _FakeCompleted(
                returncode=1,
                stderr="could not find any issue with number 9999",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert gh_provider.fetch_task(external_id="9999") is None


# ===========================================================================
# list_tasks
# ===========================================================================


class TestListTasks:
    def test_pagination_handled(self, http_provider) -> None:
        page1 = [_make_gh_issue_payload(number=n) for n in (1, 2, 3)]
        page2 = [_make_gh_issue_payload(number=n) for n in (4, 5)]
        # Use a side_effect callback so each call returns the next page
        # — respx's params-based route matching can be finicky across
        # versions; this is rock-solid and reads as a state machine.
        responses_iter = iter(
            [
                httpx.Response(
                    200,
                    json=page1,
                    headers={
                        "Link": (
                            "<https://api.github.com/repos/octo/repo/"
                            'issues?page=2>; rel="next", '
                            "<https://api.github.com/repos/octo/repo/"
                            'issues?page=2>; rel="last"'
                        )
                    },
                ),
                httpx.Response(200, json=page2),
            ]
        )

        def _next_page(_request):
            return next(responses_iter)

        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues").mock(side_effect=_next_page)
            tasks = http_provider.list_tasks()
        assert len(tasks) == 5
        assert sorted(t.external_id for t in tasks) == [
            "1",
            "2",
            "3",
            "4",
            "5",
        ]

    def test_empty_result_returns_empty_list(self, http_provider) -> None:
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues").mock(
                return_value=httpx.Response(200, json=[])
            )
            assert http_provider.list_tasks() == []

    def test_pull_requests_filtered_out(self, http_provider) -> None:
        items = [
            _make_gh_issue_payload(number=1),
            # PRs have a 'pull_request' key on the issues endpoint.
            {**_make_gh_issue_payload(number=2), "pull_request": {"url": "..."}},
            _make_gh_issue_payload(number=3),
        ]
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues").mock(
                return_value=httpx.Response(200, json=items)
            )
            tasks = http_provider.list_tasks()
        assert [t.external_id for t in tasks] == ["1", "3"]

    def test_status_label_round_trip(self, http_provider) -> None:
        # Issue with a status label set; we don't auto-map it but the
        # metadata exposes it for the reconciliation engine.
        payload = _make_gh_issue_payload(
            number=10, labels=["status:in-progress"]
        )
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues").mock(
                return_value=httpx.Response(200, json=[payload])
            )
            [t] = http_provider.list_tasks()
        labels = t.provider_metadata["labels"]
        assert "status:in-progress" in labels
        # Sanity: the label resolves back to a TaskStatus enum.
        assert LABEL_TO_STATUS["status:in-progress"] == TaskStatus.in_progress

    def test_gh_cli_list(self, monkeypatch, gh_provider) -> None:
        payloads = [
            _make_gh_issue_payload(number=1),
            _make_gh_issue_payload(number=2),
        ]

        def fake_run(argv, **kwargs):  # noqa: ARG001
            return _FakeCompleted(returncode=0, stdout=json.dumps(payloads))

        monkeypatch.setattr(subprocess, "run", fake_run)
        tasks = gh_provider.list_tasks()
        assert [t.external_id for t in tasks] == ["1", "2"]


# ===========================================================================
# delete_task
# ===========================================================================


class TestDeleteTask:
    def test_http_close_happy_path(self, http_provider) -> None:
        payload = _make_gh_issue_payload(number=42, state="closed")
        with respx.mock(base_url="https://api.github.com") as mock:
            route = mock.patch("/repos/octo/repo/issues/42").mock(
                return_value=httpx.Response(200, json=payload)
            )
            assert http_provider.delete_task(external_id="42") is None
            body = json.loads(route.calls[0].request.content)
            assert body["state"] == "closed"

    def test_http_404_is_idempotent(self, http_provider) -> None:
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.patch("/repos/octo/repo/issues/404").mock(
                return_value=httpx.Response(404, json={"message": "Not Found"})
            )
            # 404 must not raise — already gone is success.
            assert http_provider.delete_task(external_id="404") is None

    def test_gh_cli_close_happy_path(self, monkeypatch, gh_provider) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            assert "close" in argv
            return _FakeCompleted(returncode=0, stdout="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert gh_provider.delete_task(external_id="42") is None

    def test_gh_cli_404_is_idempotent(self, monkeypatch, gh_provider) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            return _FakeCompleted(
                returncode=1, stderr="could not find any issue"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert gh_provider.delete_task(external_id="9999") is None


# ===========================================================================
# health_check
# ===========================================================================


class TestHealthCheck:
    def test_gh_auth_ok_returns_available_true(
        self, monkeypatch, gh_provider
    ) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            return _FakeCompleted(returncode=0, stdout="ok\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        health = gh_provider.health_check()
        assert health.available is True
        assert health.auth_configured is True
        assert health.error is None

    def test_gh_unavailable_returns_unavailable(
        self, monkeypatch, gh_provider
    ) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            raise FileNotFoundError("gh: not found")

        monkeypatch.setattr(subprocess, "run", fake_run)
        health = gh_provider.health_check()
        assert health.available is False
        assert health.auth_configured is False
        assert health.error is not None
        assert "gh CLI unavailable" in health.error

    def test_gh_unauthed_returns_available_but_no_auth(
        self, monkeypatch, gh_provider
    ) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            if "--version" in argv:
                return _FakeCompleted(returncode=0, stdout="gh 2.0\n")
            if "auth" in argv and "status" in argv:
                return _FakeCompleted(returncode=1, stderr="not logged in")
            return _FakeCompleted(returncode=0, stdout="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        health = gh_provider.health_check()
        assert health.available is True
        assert health.auth_configured is False
        assert health.error is not None
        assert "gh auth login" in health.error

    def test_http_with_token_returns_authed(self, http_provider) -> None:
        health = http_provider.health_check()
        assert health.available is True
        assert health.auth_configured is True
        assert health.error is None

    def test_http_without_token_returns_no_auth(self, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        provider = GitHubIssuesProvider(repo="octo/repo", transport="http")
        health = provider.health_check()
        assert health.available is True
        assert health.auth_configured is False
        assert health.error is not None
        assert "GITHUB_TOKEN" in health.error

    def test_http_with_env_token_returns_authed(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_env_token")
        provider = GitHubIssuesProvider(repo="octo/repo", transport="http")
        health = provider.health_check()
        assert health.auth_configured is True

    def test_health_check_never_raises_on_unexpected(
        self, gh_provider
    ) -> None:
        # Patch deep enough that ANY exception bubbles up to the catch-all.
        with patch.object(
            gh_provider,
            "_make_gh_client",
            side_effect=RuntimeError("unexpected"),
        ):
            health = gh_provider.health_check()
            assert health.available is False
            assert "unexpected" in (health.error or "")


# ===========================================================================
# Status / label mapping
# ===========================================================================


class TestStatusLabelMapping:
    def test_every_task_status_has_a_label(self) -> None:
        for status in TaskStatus:
            assert status in STATUS_TO_LABEL, (
                f"TaskStatus.{status.name} missing from STATUS_TO_LABEL"
            )

    def test_no_duplicate_labels(self) -> None:
        labels = list(STATUS_TO_LABEL.values())
        assert len(labels) == len(set(labels)), "duplicate label in mapping"

    def test_all_labels_prefixed_status_colon(self) -> None:
        for label in STATUS_TO_LABEL.values():
            assert label.startswith("status:"), (
                f"label {label!r} missing status: prefix"
            )

    def test_round_trip_lossless(self) -> None:
        for status, label in STATUS_TO_LABEL.items():
            assert LABEL_TO_STATUS[label] == status

    def test_label_to_status_covers_all_labels(self) -> None:
        assert set(LABEL_TO_STATUS) == set(STATUS_TO_LABEL.values())

    def test_done_is_the_only_closed_status(self) -> None:
        assert DONE_STATUSES == frozenset({TaskStatus.done})

    def test_rejected_stays_open(self) -> None:
        # Rejected tasks must NOT close the issue — humans need to see them.
        assert TaskStatus.rejected not in DONE_STATUSES

    @pytest.mark.parametrize(
        "status,label",
        [
            (TaskStatus.proposed, "status:proposed"),
            (TaskStatus.drafted, "status:drafted"),
            (TaskStatus.reviewed, "status:reviewed"),
            (TaskStatus.ready, "status:ready"),
            (TaskStatus.claimed, "status:claimed"),
            (TaskStatus.in_progress, "status:in-progress"),
            (TaskStatus.blocked, "status:blocked"),
            (TaskStatus.needs_review, "status:needs-review"),
            (TaskStatus.accepted, "status:accepted"),
            (TaskStatus.done, "status:done"),
            (TaskStatus.rejected, "status:rejected"),
        ],
    )
    def test_status_label_explicit(
        self, status: TaskStatus, label: str
    ) -> None:
        assert STATUS_TO_LABEL[status] == label


# ===========================================================================
# Provider metadata round-trip
# ===========================================================================


class TestProviderMetadata:
    def test_fetch_populates_provider_metadata(self, http_provider) -> None:
        payload = _make_gh_issue_payload(
            number=42,
            labels=["status:in-progress", "bug", "p1"],
            assignees=["octocat", "hubot"],
        )
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues/42").mock(
                return_value=httpx.Response(200, json=payload)
            )
            task = http_provider.fetch_task(external_id="42")
        assert task is not None
        meta = task.provider_metadata
        assert meta["labels"] == ["status:in-progress", "bug", "p1"]
        assert meta["assignees"] == ["octocat", "hubot"]
        assert meta["issue_number"] == 42
        assert meta["issue_node_id"] == "node_42"

    def test_metadata_survives_extra_forbid_boundary(
        self, http_provider
    ) -> None:
        # ExternalTask has extra="forbid". Verify our metadata dict passes
        # validation when carried inside the dict-typed field.
        payload = _make_gh_issue_payload(number=1)
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues/1").mock(
                return_value=httpx.Response(200, json=payload)
            )
            task = http_provider.fetch_task(external_id="1")
        assert task is not None
        # Re-construct via model_dump → ExternalTask(**dump) to prove the
        # extra="forbid" boundary is intact.
        rebuilt = ExternalTask(**task.model_dump())
        assert rebuilt == task

    def test_create_returns_ref_whose_external_id_matches_payload(
        self, http_provider
    ) -> None:
        payload = _make_gh_issue_payload(number=77)
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.post("/repos/octo/repo/issues").mock(
                return_value=httpx.Response(201, json=payload)
            )
            ref = http_provider.push_task(task=_make_task(), mapping=None)
        assert ref.external_id == "77"
        assert ref.provider_id == "github_issues"

    def test_body_footer_round_trip(self) -> None:
        body = _compose_body("My description", "T042")
        assert body.endswith("_synced from fakoli-state task T042_")
        assert _strip_footer(body) == "My description"

    def test_strip_footer_preserves_unrelated_body(self) -> None:
        assert _strip_footer("no footer here") == "no footer here"
        # A body that just looks similar but is not the exact pattern.
        assert _strip_footer("---\nsynced from elsewhere\n") == (
            "---\nsynced from elsewhere\n"
        )


# ===========================================================================
# Datetime parsing
# ===========================================================================


class TestParseGithubDatetime:
    def test_z_suffix_parses_to_utc(self) -> None:
        dt = _parse_github_datetime("2026-05-25T12:00:00Z")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == datetime.timedelta(0)

    def test_offset_normalised_to_utc(self) -> None:
        dt = _parse_github_datetime("2026-05-25T14:00:00+02:00")
        assert dt == datetime.datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)

    def test_malformed_raises_sync_provider_error(self) -> None:
        with pytest.raises(SyncProviderError, match="malformed timestamp"):
            _parse_github_datetime("not-a-date")


# ===========================================================================
# gh CLI client edge cases
# ===========================================================================


class TestGhCliClient:
    def test_requires_owner_slash_repo(self) -> None:
        with pytest.raises(ValueError, match="<owner>/<repo>"):
            GhCliClient(repo="bad")

    def test_malformed_json_raises_sync_provider_error(
        self, monkeypatch
    ) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            return _FakeCompleted(
                returncode=0, stdout="not json"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = GhCliClient(repo="octo/repo")
        with pytest.raises(SyncProviderError, match="malformed JSON"):
            client.view_issue(number="1")

    def test_rate_limit_phrase_classified(self, monkeypatch) -> None:
        def fake_run(argv, **kwargs):  # noqa: ARG001
            return _FakeCompleted(
                returncode=1, stderr="API rate limit exceeded for user"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = GhCliClient(repo="octo/repo")
        with pytest.raises(RateLimitExceeded):
            client.view_issue(number="1")


# ===========================================================================
# HTTP client edge cases
# ===========================================================================


class TestGithubHttpClient:
    def test_requires_owner_slash_repo(self) -> None:
        with pytest.raises(ValueError, match="<owner>/<repo>"):
            GithubHttpClient(repo="bad")

    def test_429_raises_rate_limit_exceeded(self) -> None:
        client = GithubHttpClient(repo="octo/repo", token="x")
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues/1").mock(
                return_value=httpx.Response(
                    429, json={"message": "Too Many Requests"}
                )
            )
            with pytest.raises(RateLimitExceeded):
                client.get_issue(number="1")

    def test_500_raises_provider_unavailable(self) -> None:
        client = GithubHttpClient(repo="octo/repo", token="x")
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.get("/repos/octo/repo/issues/1").mock(
                return_value=httpx.Response(500, json={"message": "internal"})
            )
            with pytest.raises(ProviderUnavailable, match="server error"):
                client.get_issue(number="1")

    def test_env_token_picked_up(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
        client = GithubHttpClient(repo="octo/repo")
        assert client.has_token() is True

    def test_no_token_returns_false_has_token(self, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        client = GithubHttpClient(repo="octo/repo")
        assert client.has_token() is False
