# Live GitHub integration tests

> **Status: designed, not yet committed — no such workflow exists in
> `.github/workflows` yet.** The tests themselves are committed and
> marker-gated, but the nightly job described below has not landed. Until it
> does, the "Running locally" runbook is the only way to exercise these
> tests, and the "Enabling on a fork / repo" steps have no effect.

The anvil plugin ships a small suite of live tests — intended to run
nightly in CI — that exercises the
real GitHub Issues REST API. It catches upstream contract drift -- label
format changes, deprecated endpoints, header renames, REST PATCH semantics
shifts -- before users hit them. These tests are marker-gated and excluded
from the default `uv run --project bin pytest` run.

This page is the operator runbook: how to enable the nightly job, how to run
the same tests locally, and what residue they leave behind in the test repo.

## Workflow

The job is designed to live at `.github/workflows/anvil-live-github.yml`. It:

- Runs on `cron: 0 6 * * *` (06:00 UTC = 22:00 PST / 02:00 EST).
- Can be triggered manually via the GitHub Actions "workflow_dispatch" UI.
- Uses `concurrency: anvil-live-github` so a manual run cancels a
  still-running nightly.
- Holds only the minimal `contents: read` permission.

## Enabling on a fork / repo

The job no-ops with a notice when the secret is unset, so a fresh fork stays
green automatically. To activate it:

### 1. Repository secret

Add a repository secret named `ANVIL_TEST_GH_TOKEN`. The token must
be a fine-grained PAT (or classic PAT) with these scopes against the test
repo:

- `repo:read` -- listing and reading issues
- `issues:write` -- creating, updating, closing, and commenting on issues

Path: **Settings -> Secrets and variables -> Actions -> New repository
secret**.

### 2. Repository variable (optional)

Add a repository **variable** (not secret) named `ANVIL_TEST_REPO`
pointing at the `<owner>/<repo>` slug of the scratch repo the tests should
exercise. If unset the workflow defaults to `fakoli/anvil-sync-test`.

Path: **Settings -> Secrets and variables -> Actions -> Variables tab ->
New repository variable**.

Use a dedicated scratch repo. The tests create real GitHub issues and the
cleanup is best-effort (see below).

## Running locally

```bash
export GITHUB_TOKEN=ghp_...                       # your test-repo PAT
export ANVIL_TEST_REPO=fakoli/anvil-sync-test
uv run --project bin pytest -m live_github -v
```

The default `uv run --project bin pytest` continues to exclude live tests via
the `addopts` filter in the repository-root `pytest.ini`. You must pass
`-m live_github` explicitly to opt in.

## What the tests cover

| Test | Surface exercised |
|---|---|
| `test_create_then_fetch_then_close_then_delete` | Full lifecycle: create issue -> fetch -> rename -> close via `status:done` -> verify both the `closed` state and the `status:done` label landed |
| `test_label_preservation_in_update` | Regression coverage for the PATCH-labels-replaces-all gotcha -- a status push must preserve user-added labels (e.g. `bug`, `area/*`) |
| `test_rate_limit_handling` | `health_check()` returns sensible values against the real API and skips cleanly when the runner cannot reach `api.github.com` |

The transport is pinned to `http` so the tests stay deterministic regardless
of whether `gh` is installed or authenticated on the runner. (The workflow
installs `gh` anyway so a future test that exercises the `gh_cli` transport
against the live API can land without a workflow change.)

## Residue in the test repo

Every test names the issues it creates with a `[fakoli-test]` prefix plus
a fresh 8-character UUID slug, e.g. `[fakoli-test] live smoke 1a2b3c4d`. The
teardown:

1. Posts a `TEST CLEANUP` comment naming the test that owned the issue.
2. Closes the issue.

GitHub does not expose an issue-delete endpoint, so closed-and-tagged is the
strongest guarantee the cleanup can give. The `[fakoli-test]` prefix makes
orphans (from a CI run that died mid-test before teardown ran) trivially
searchable. An operator can sweep orphans older than 7 days by searching the
test repo for `is:issue is:open [fakoli-test] created:<7d ago` and closing
them manually.

The `test_label_preservation_in_update` test also creates a per-run scratch
label named `fakoli-test-bug-<suffix>`. These accumulate but do not affect
the test repo's primary labels (`bug`, `enhancement`, etc.). Sweep them via
the repo's Labels page when the count gets noisy.

## Troubleshooting

- **Workflow notice "Live GitHub tests skipped"** -- the
  `ANVIL_TEST_GH_TOKEN` secret is unset on this repo. Add it (see
  above) or accept the skip if drift detection is not desired here.
- **All tests `SKIPPED` locally** -- you did not export `GITHUB_TOKEN` or
  `ANVIL_TEST_REPO`. The marker gate accepts the run; the fixtures
  defensively skip when the env is incomplete.
- **`AuthenticationFailed` on the first call** -- token lacks
  `issues:write` on the configured `ANVIL_TEST_REPO`. Either widen
  the token scopes or point `ANVIL_TEST_REPO` at a repo the token
  can write to.
- **`RateLimitExceeded`** -- nightly runs are well below the 5000-req/hr
  primary limit, so a hit usually means another test or scratch script is
  hammering the same token. Wait for the reset window (logged in the
  exception message) and re-run via `workflow_dispatch`.
- **Orphans accumulating** -- a CI run was killed mid-test. Search the test
  repo for the `[fakoli-test]` prefix on open issues and close them; this
  is intentionally a manual sweep, not a destructive auto-cleanup.
