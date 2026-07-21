"""Live integration smoke tests for :class:`GitHubIssuesProvider`.

Phase 8 Wave 4 Task 8 -- nightly CI drift detection.

Every test in this module is decorated with ``@pytest.mark.live_github`` and
is EXCLUDED from the default ``uv run --project bin pytest`` run by the
``addopts`` filter in the repository-root ``pytest.ini``. They only run when
explicitly selected via
``-m live_github`` and are intended to be invoked nightly by the
``.github/workflows/anvil-live-github.yml`` workflow against a real
GitHub test repo. The intent is to catch upstream API drift -- label format
changes, deprecated endpoints, header renames -- before users do.

Required env when running:
    - ``GITHUB_TOKEN``: PAT with ``repo:read`` + ``issues:write`` on the
      target test repo. In CI this is ``secrets.ANVIL_TEST_GH_TOKEN``.
    - ``ANVIL_TEST_REPO``: ``<owner>/<repo>`` of the scratch repo the
      tests are allowed to create / close issues in. Defaults are intentionally
      absent -- without an explicit repo the tests refuse to run.

Cleanup contract
----------------
Each test names the issues it creates with a ``[fakoli-test]`` prefix plus a
fresh UUID, and closes them in the test teardown. GitHub does not expose a
delete endpoint for issues, so a closed-and-tagged paper trail is the most
the cleanup can do. Orphans are searchable in the test repo by the prefix.
"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest

from anvil.state.models import Task, TaskPriority, TaskStatus
from anvil.sync.clients.github_http import GithubHttpClient
from anvil.sync.providers.github_issues import (
    LABEL_TO_STATUS,
    STATUS_TO_LABEL,
    GitHubIssuesProvider,
)

# Module-level marker: every test below carries the `live_github` mark. We
# apply it via pytestmark instead of decorating each test individually so a
# future test added here does not silently land in the default suite.
pytestmark = pytest.mark.live_github


UTC = datetime.UTC

# Issue title prefix the cleanup sweep keys off. Anything in the test repo
# matching this prefix is fair game to manually close if a CI run dies before
# its teardown ran.
_TEST_PREFIX = "[fakoli-test]"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_repo() -> str:
    """``<owner>/<repo>`` from env; skip if unset.

    The marker filter is the canonical gate for whether these tests run; the
    skip here is a defensive belt for the rare case where a developer runs
    ``pytest -m live_github`` locally without first exporting the env vars.
    """
    repo = os.environ.get("ANVIL_TEST_REPO")
    if not repo:
        pytest.skip("ANVIL_TEST_REPO not set")
    return repo


@pytest.fixture
def test_token() -> str:
    """PAT from env; skip if unset (same rationale as :func:`test_repo`)."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        pytest.skip("GITHUB_TOKEN not set")
    return token


@pytest.fixture
def provider(test_repo: str, test_token: str) -> GitHubIssuesProvider:
    """A provider forced onto the HTTP transport.

    The nightly CI runner does not authenticate ``gh``, so we pin the
    transport explicitly rather than letting ``auto`` probe and surprise us.
    """
    return GitHubIssuesProvider(
        repo=test_repo,
        transport="http",
        token=test_token,
    )


@pytest.fixture
def http_client(test_repo: str, test_token: str) -> GithubHttpClient:
    """Raw HTTP client used for cleanup and for direct label manipulation
    in :func:`test_label_preservation_in_update`. Closed in the teardown.
    """
    client = GithubHttpClient(repo=test_repo, token=test_token)
    yield client
    client.close()


def _now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def _unique_suffix() -> str:
    """Short UUID slug used to make issue titles globally unique."""
    return uuid.uuid4().hex[:8]


def _make_task(
    *,
    suffix: str,
    title: str | None = None,
    description: str = "synthetic body from anvil live tests",
    status: TaskStatus = TaskStatus.in_progress,
) -> Task:
    """Build a synthetic Task. Uses the prefix + suffix so cleanup is greppable."""
    if title is None:
        title = f"{_TEST_PREFIX} live smoke {suffix}"
    return Task(
        id=f"T-live-{suffix}",
        feature_id="F-live",
        title=title,
        description=description,
        status=status,
        priority=TaskPriority.medium,
        created_at=_now(),
        updated_at=_now(),
    )


def _safe_close(client: GithubHttpClient, number: str) -> None:
    """Close issue ``number``; swallow not-found.

    Used in teardown chains where the issue may already be closed by the
    test body itself. We do not want a teardown failure to obscure the
    real assertion failure (if any) from the test body.
    """
    try:
        client.close_issue(number=number)
    except Exception:  # noqa: BLE001 -- best-effort cleanup
        pass


def _add_cleanup_comment(
    client: GithubHttpClient, number: str, marker: str
) -> None:
    """Post a ``TEST CLEANUP`` comment so a human auditing the repo sees
    which run owned the issue. Best-effort; failure is ignored.
    """
    try:
        client._request(
            "POST",
            f"/repos/{client.repo}/issues/{number}/comments",
            json_body={
                "body": f"TEST CLEANUP -- {marker} -- closing synthetic issue",
            },
        )
    except Exception:  # noqa: BLE001
        pass


# ===========================================================================
# Full lifecycle smoke
# ===========================================================================


def test_create_then_fetch_then_close_then_delete(
    provider: GitHubIssuesProvider, http_client: GithubHttpClient
) -> None:
    """End-to-end create -> fetch -> update -> close against the real API.

    This is the load-bearing live test: it exercises every method on the
    provider against a real GitHub repo. Any upstream contract change (label
    format, body footer rendering, state transitions, label-on-PATCH
    replacement semantics) will surface here.
    """
    suffix = _unique_suffix()
    task = _make_task(suffix=suffix, status=TaskStatus.in_progress)
    created_ref = None
    try:
        # ---- 1. create -----------------------------------------------------
        created_ref = provider.push_task(task=task, mapping=None)
        assert created_ref.provider_id == "github_issues"
        assert created_ref.external_id.isdigit(), (
            f"GitHub returned non-numeric issue id: {created_ref.external_id!r}"
        )
        assert created_ref.url is not None
        assert created_ref.url.startswith("https://github.com/")

        # ---- 2. fetch ------------------------------------------------------
        fetched = provider.fetch_task(external_id=created_ref.external_id)
        assert fetched is not None, "freshly created issue was not findable"
        assert fetched.external_id == created_ref.external_id
        assert fetched.title == task.title
        # The footer is stripped on the way out so the body the agent sent
        # round-trips byte-for-byte.
        assert fetched.body == task.description
        # Labels surface in provider_metadata; the status label MUST be there.
        labels = fetched.provider_metadata.get("labels", [])
        assert STATUS_TO_LABEL[TaskStatus.in_progress] in labels, (
            f"expected status:in-progress label on fresh issue; got {labels!r}"
        )
        # status_label here is the GitHub-native open/closed bit.
        assert fetched.status_label == "open"

        # ---- 3. update title ----------------------------------------------
        new_title = f"{_TEST_PREFIX} live smoke {suffix} (renamed)"
        task = task.model_copy(update={"title": new_title, "updated_at": _now()})
        updated_ref = provider.push_task(task=task, mapping=created_ref)
        # Updating must not rotate the external id -- GitHub issue numbers
        # are immutable once assigned.
        assert updated_ref.external_id == created_ref.external_id
        re_fetched = provider.fetch_task(external_id=created_ref.external_id)
        assert re_fetched is not None
        assert re_fetched.title == new_title

        # ---- 4. close via status:done -------------------------------------
        task = task.model_copy(
            update={"status": TaskStatus.done, "updated_at": _now()}
        )
        provider.push_task(task=task, mapping=created_ref)
        closed = provider.fetch_task(external_id=created_ref.external_id)
        assert closed is not None
        assert closed.status_label == "closed", (
            f"expected closed state after status:done push; got {closed.status_label!r}"
        )
        closed_labels = closed.provider_metadata.get("labels", [])
        assert STATUS_TO_LABEL[TaskStatus.done] in closed_labels, (
            f"expected status:done label on closed issue; got {closed_labels!r}"
        )
        # Old status label must be gone -- the update path removes managed
        # status:* labels other than the new one.
        assert STATUS_TO_LABEL[TaskStatus.in_progress] not in closed_labels
    finally:
        # ---- 5. cleanup ----------------------------------------------------
        if created_ref is not None:
            _add_cleanup_comment(
                http_client, created_ref.external_id, marker=f"lifecycle-{suffix}"
            )
            _safe_close(http_client, created_ref.external_id)


# ===========================================================================
# Label preservation -- SF-3 regression coverage against the live API
# ===========================================================================


def test_label_preservation_in_update(
    provider: GitHubIssuesProvider, http_client: GithubHttpClient
) -> None:
    """A status update must NOT clobber user-added labels.

    PATCH /issues/{n} with ``labels=[...]`` replaces the entire labels array
    on the GitHub side, so the provider has to fetch existing labels and
    preserve every non-``status:*`` one before sending the PATCH. SF-3 fixed
    this against the mock-shaped tests; this exercises the same path against
    the real API so a server-side semantics change is caught.
    """
    suffix = _unique_suffix()
    task = _make_task(suffix=suffix, status=TaskStatus.in_progress)
    user_label = f"fakoli-test-bug-{suffix}"
    created_ref = None
    try:
        # 1. create with status:in-progress
        created_ref = provider.push_task(task=task, mapping=None)

        # 2. ensure the user label exists, then attach it directly via REST.
        #    create_label is idempotent (422 if it exists) so we swallow that.
        try:
            http_client._request(
                "POST",
                f"/repos/{http_client.repo}/labels",
                json_body={
                    "name": user_label,
                    "color": "ededed",
                    "description": "anvil live test scratch label",
                },
            )
        except Exception:  # noqa: BLE001 -- 422 already-exists is fine
            pass
        http_client._request(
            "POST",
            f"/repos/{http_client.repo}/issues/{created_ref.external_id}/labels",
            json_body={"labels": [user_label]},
        )

        # 3. push a status change (in-progress -> needs-review). The provider
        #    has to preserve the user-added label across this PATCH.
        task = task.model_copy(
            update={"status": TaskStatus.needs_review, "updated_at": _now()}
        )
        provider.push_task(task=task, mapping=created_ref)

        # 4. fetch and assert both labels survive.
        after = provider.fetch_task(external_id=created_ref.external_id)
        assert after is not None
        labels_after = set(after.provider_metadata.get("labels", []))
        assert user_label in labels_after, (
            f"user label {user_label!r} was clobbered by status push; "
            f"survivors: {sorted(labels_after)!r}"
        )
        assert STATUS_TO_LABEL[TaskStatus.needs_review] in labels_after
        # And the old status label is gone.
        assert STATUS_TO_LABEL[TaskStatus.in_progress] not in labels_after
        # Sanity: every surviving status:* label round-trips through the
        # reverse map without KeyError (catches a server-side rename).
        for label in labels_after:
            if label.startswith("status:"):
                assert label in LABEL_TO_STATUS, (
                    f"unknown status label {label!r} from GitHub; "
                    "STATUS_TO_LABEL mapping may need updating"
                )
    finally:
        if created_ref is not None:
            _add_cleanup_comment(
                http_client, created_ref.external_id, marker=f"label-{suffix}"
            )
            _safe_close(http_client, created_ref.external_id)


# ===========================================================================
# Health check / rate-limit awareness
# ===========================================================================


def test_rate_limit_handling(provider: GitHubIssuesProvider) -> None:
    """``health_check`` must return sensible values against the real API.

    Best-effort: if the test runner cannot reach api.github.com at all, the
    method still returns a populated ProviderHealth (it MUST NOT raise per
    Protocol contract) and we simply skip on ``available=False`` so a
    runner-side network outage does not red the nightly cron.
    """
    health = provider.health_check()
    assert health.last_check_at is not None
    if not health.available:
        pytest.skip(
            f"runner cannot reach GitHub API: {health.error!r}; "
            "treating as transient infrastructure issue, not a code regression"
        )
    # Token came from the fixture, so auth_configured must be true.
    assert health.auth_configured is True, (
        f"expected auth_configured=True with GITHUB_TOKEN set; got error={health.error!r}"
    )
    assert health.error is None
