# fakoli-state sync providers

## Why a Protocol

fakoli-state needs to talk to many task-tracker backends — GitHub Issues,
GitHub Projects, Linear, Monday, Jira, internal trackers — without
re-implementing the state engine, reconciliation, CLI, or audit log for
each one. The `SyncProvider` Protocol is the single shape every backend
implements; the engine targets the abstraction, not any one vendor.

This document is for contributors adding a new provider. End-user
documentation for the bundled GitHub Issues provider lives in
[`github-sync.md`](github-sync.md).

---

## The `SyncProvider` Protocol

Defined in `bin/src/fakoli_state/sync/provider.py`. Use
`typing.Protocol` — runtime structural typing, no inheritance required.

```python
from typing import Protocol

class SyncProvider(Protocol):
    provider_id: str          # registry key, snake_case
    display_name: str         # human-facing name for CLI output

    def push_task(
        self, *, task: Task, mapping: ExternalRef | None,
    ) -> ExternalRef: ...

    def fetch_task(self, *, external_id: str) -> ExternalTask | None: ...

    def list_tasks(self) -> list[ExternalTask]: ...

    def delete_task(self, *, external_id: str) -> None: ...

    def health_check(self) -> ProviderHealth: ...
```

### Method contracts

| Method         | Contract                                                                                                 |
|----------------|----------------------------------------------------------------------------------------------------------|
| `push_task`    | Create when `mapping is None`, update when not. Return an `ExternalRef` pointing at the persisted record.|
| `fetch_task`   | Return the current remote payload as `ExternalTask`, or `None` if the record no longer exists (tombstone).|
| `list_tasks`   | Return every remote record in scope. Implementations handle pagination transparently.                    |
| `delete_task`  | Make the record no longer present in `list_tasks` output. Treat 404 / already-absent as success.         |
| `health_check` | Probe reachability + auth. **MUST NOT raise** — return a `ProviderHealth` with `available=False` instead.|

### Discipline

- **Keyword-only after `self`.** Same as `LLMProvider`. Positional args
  break the moment a new optional kwarg is added; every contributor
  re-types these signatures, so the boundary noise is worth it.
- **Single exception type to catch.** Every failure path MUST wrap the
  underlying error in `SyncProviderError` (or a subclass) via
  `raise SyncProviderError(...) from exc`. Callers `except
  SyncProviderError` once and recover the original via `exc.__cause__`.
- **`extra="forbid"` on every Pydantic model.** Silent field drift is the
  abstraction's worst failure mode; catch it at the boundary, not in
  storage.
- **Idempotency.** Sync is a polling loop. Calling `push_task` twice with
  the same `(task, mapping)` should land the same remote state both
  times.

---

## The supporting models

All three live in `sync/provider.py` and use `ConfigDict(extra="forbid")`.

### `ExternalRef`

Minimal pointer to a remote record. Stored on the `SyncMapping` row.

| Field         | Type           | Notes                                                                  |
|---------------|----------------|------------------------------------------------------------------------|
| `provider_id` | `str`          | Registry key (snake_case). Must match a key in `PROVIDER_REGISTRY`.    |
| `external_id` | `str`          | Provider-native id, always stringified (e.g. `"42"`, `"ENG-123"`).     |
| `url`         | `str \| None`  | Optional human-facing URL. Not load-bearing.                           |

### `ExternalTask`

Full payload returned by `fetch_task` / `list_tasks`.

| Field               | Type                  | Notes                                                                                            |
|---------------------|-----------------------|--------------------------------------------------------------------------------------------------|
| `external_id`       | `str`                 | Same shape as `ExternalRef.external_id`.                                                         |
| `title`             | `str`                 | Empty string allowed; `None` is not.                                                             |
| `body`              | `str`                 | Markdown-flavoured for every current target. Defaults to `""`.                                   |
| `status_label`      | `str \| None`         | Provider-native status (e.g. `"open"`, `"In Progress"`). Mapping to `TaskStatus` is provider work.|
| `url`               | `str \| None`         | Human-facing URL.                                                                                |
| `last_modified`     | `datetime` (tz-aware) | Drives conflict detection. Naive datetimes are rejected at the validator.                        |
| `provider_metadata` | `dict[str, Any]`      | Provider-specific extension blob (labels, assignees, custom fields). See best practice below.    |

`provider_metadata` is the extension point for fields that don't fit a
generic abstraction. GitHub puts `{"labels": [...], "assignees": [...],
"issue_number": ..., "issue_node_id": ...}` here; Jira would put
`{"watchers": [...], "reporter": ...}`; Monday puts people-column shapes.
The reconciliation engine treats this dict as opaque — only the
originating provider knows the shape.

### `ProviderHealth`

Returned by `health_check`. Never `raise`; encode failure as fields.

| Field             | Type            | Notes                                                                  |
|-------------------|-----------------|------------------------------------------------------------------------|
| `available`       | `bool`          | Upstream reachable. Independent of auth.                               |
| `auth_configured` | `bool`          | Valid credentials present.                                             |
| `last_check_at`   | `datetime` (UTC)| When this snapshot was taken.                                          |
| `error`           | `str \| None`   | Human-readable explanation. Surfaced verbatim by the CLI; keep short.  |

---

## Registry mechanics

Defined in `sync/registry.py`.

```python
from fakoli_state.sync.registry import register_sync_provider

register_sync_provider("linear_issues", LinearIssuesProvider)
```

`register_sync_provider` raises `ValueError` on empty `provider_id` and
on duplicate registration (silent overwrite is how plugins shadow each
other in production — refuse it).

### Auto-registration via side-effect import

The canonical pattern: every provider module calls
`register_sync_provider(...)` at module scope (bottom of file, after the
class is bound), and the package `__init__.py` imports each provider
submodule so registrations fire on package load.

See `sync/providers/github_issues.py:613` for the registration call and
`sync/providers/__init__.py` for the side-effect import.

---

## Step-by-step: add Linear support

```
bin/src/fakoli_state/sync/providers/linear.py        # new provider class
bin/src/fakoli_state/sync/clients/linear_api.py      # API client (GraphQL)
tests/test_linear_provider.py                        # respx-based tests
```

### 1. Write the API client

`sync/clients/linear_api.py` wraps the GraphQL endpoint via `httpx`.
Keep it transport-only: no `Task` ↔ Linear-issue translation, no
fakoli-state types. Each method returns a raw dict and raises
`SyncProviderError` on transport failure.

### 2. Write the provider

`sync/providers/linear.py` consumes the client and exposes the Protocol:

```python
from fakoli_state.sync.provider import ExternalRef, ExternalTask, ProviderHealth
from fakoli_state.sync.registry import register_sync_provider

class LinearIssuesProvider:
    provider_id: str = "linear_issues"
    display_name: str = "Linear"

    def __init__(self, *, team_id: str | None = None) -> None:
        ...

    def push_task(self, *, task, mapping): ...
    def fetch_task(self, *, external_id): ...
    def list_tasks(self): ...
    def delete_task(self, *, external_id): ...
    def health_check(self): ...

register_sync_provider(LinearIssuesProvider.provider_id, LinearIssuesProvider)
```

### 3. Register on package load

Add to `sync/providers/__init__.py`:

```python
from fakoli_state.sync.providers import linear  # noqa: F401
```

### 4. Test with `respx` + `RecordedSyncProvider`

`tests/test_linear_provider.py` uses `respx` to mock the HTTP layer for
provider-specific tests; CLI / engine tests that consume *any* provider
use `RecordedSyncProvider` instead (see Testing pattern below).

After landing those four files, `fakoli-state sync provider
linear_issues` works end-to-end with no changes to the CLI, engine, or
reconciliation code.

---

## Status label mapping

Every provider must map fakoli-state's 11 `TaskStatus` values to whatever
the remote system uses. Centralise the mapping in a module-level
`STATUS_TO_LABEL: dict[TaskStatus, str]` and a reverse `LABEL_TO_STATUS`
so push and fetch agree verbatim.

The GitHub Issues mapping (canonical reference) — see
`sync/providers/github_issues.py:67`:

```python
STATUS_TO_LABEL: dict[TaskStatus, str] = {
    TaskStatus.proposed:     "status:proposed",
    TaskStatus.drafted:      "status:drafted",
    TaskStatus.reviewed:     "status:reviewed",
    TaskStatus.ready:        "status:ready",
    TaskStatus.claimed:      "status:claimed",
    TaskStatus.in_progress:  "status:in-progress",
    TaskStatus.blocked:      "status:blocked",
    TaskStatus.needs_review: "status:needs-review",
    TaskStatus.accepted:     "status:accepted",
    TaskStatus.done:         "status:done",
    TaskStatus.rejected:     "status:rejected",
}
```

Where the provider has a separate open/closed bit (GitHub, Jira), keep
a `DONE_STATUSES: frozenset[TaskStatus]` so closure semantics live next
to the label mapping.

---

## Testing pattern

Two complementary doubles:

| Test target                                      | Use                                                                       |
|--------------------------------------------------|---------------------------------------------------------------------------|
| CLI / state-engine code that consumes a provider | `RecordedSyncProvider` (`sync/recorded.py`). Hash-keyed canned responses. |
| Provider-specific HTTP / subprocess paths        | `respx` for `httpx` clients; `subprocess.run` patches for CLI subprocesses.|

### `RecordedSyncProvider`

```python
from fakoli_state.sync.recorded import RecordedSyncProvider
from fakoli_state.sync.provider import ExternalRef

key = RecordedSyncProvider.record_key(
    "push_task", task=task, mapping=None,
)
provider = RecordedSyncProvider(
    provider_id="linear_issues",
    display_name="Linear (test)",
    recordings={
        key: ExternalRef(
            provider_id="linear_issues", external_id="ENG-123", url=None,
        ),
    },
)
```

On a key miss the provider raises `SyncProviderError` — accidental
"this test secretly called the real API" failures are loud, not silent.

### No live API calls

Live HTTP / GraphQL calls outside `@pytest.mark.live_github` (or its
provider equivalent) are forbidden. The default test run
(`uv run pytest -q`) excludes live markers; the nightly CI workflow runs
them with secrets.

---

## Per-provider configuration (v1.9.0)

The `sync` CLI iterates *configured providers* — by default every provider
in `PROVIDER_REGISTRY`, optionally narrowed to an explicit subset via
`config.yaml`. v1.9.0 added the `sync.providers` top-level config key so
projects can opt into a deliberate subset (or out of every provider) without
deregistering modules.

### Schema

```yaml
# .fakoli-state/config.yaml — fragment
sync:
  providers:
    - github_issues
    # - linear_issues      # contributor-registered providers also accepted
    # - monday_boards
```

### Three-way semantics

The presence-vs-absence-vs-empty-list distinction is load-bearing:

| YAML form                | `Config.sync_providers`  | Caller behaviour                                            |
|--------------------------|--------------------------|-------------------------------------------------------------|
| key absent               | `None`                   | Fall back to `sorted(PROVIDER_REGISTRY)` (v1.8.0 default).  |
| `sync.providers: [a, b]` | `("a", "b")`             | Use the explicit list, in order.                            |
| `sync.providers: []`     | `()` (NOT `None`)        | Opt out of every provider — sync is a no-op.                |

The `()` vs `None` distinction matters: a frozen project that wants to
suppress sync drift entirely needs `[]` (an explicit empty list); a
project that simply has not bothered to configure providers should still
scan everything registered. The `Config.sync_providers: tuple[str, ...]
| None` field pins both behaviours; tests cover both.

### Fallback safety

Lookup happens in `_resolve_configured_providers` in `cli/sync.py` —
the single seam. A malformed config (unparseable YAML, type errors)
falls back to `sorted(PROVIDER_REGISTRY)` rather than breaking
`fakoli-state sync` entirely. Loud config errors are the job of
`fakoli-state init` / `doctor`, not the sync surface.

### Init template

The `fakoli-state init` config template does NOT include `sync.providers`
— it is opt-in. Add it manually when you want to narrow or opt out.

### Reconciliation interaction

The bare `fakoli-state sync` (reconciliation only) consumes the same
configured-providers list. When the list is the registry fallback,
reconciliation emits one `missing_sync_mapping` discrepancy per missing
provider per done task. When the list is explicit, only the listed
providers contribute discrepancy rows — the "frozen project" mode above
silences `missing_sync_mapping` entirely.

---

## `provider_id` naming

**Snake_case.** Always. Examples:

- `github_issues`
- `github_projects`
- `linear_issues`
- `monday_boards`
- `jira_issues`

The `ExternalSystem` enum and the `sync_mappings.external_system` DB
column take snake_case values. Kebab-case (`github-issues`) registers
and looks up fine in the registry dict but fails at storage time with
confusing reconciliation errors downstream. The Wave 1 critic flagged
this twice; the registry's docstring repeats the warning.

---

## `provider_metadata` best practice

| Goes on top-level `ExternalTask` fields | Goes in `provider_metadata` dict           |
|-----------------------------------------|--------------------------------------------|
| `title`, `body`, `status_label`, `url`  | Labels (provider-shaped list)              |
| `last_modified`                         | Assignees (provider-shaped list)           |
| `external_id`                           | Custom fields, watchers, reporter          |
|                                         | Internal IDs (issue node id, etc.)         |
|                                         | Provider-specific timestamps               |

Rule of thumb: if the reconciliation engine needs to read the field to
make a decision (title diff, body diff, status comparison, recency
check), it belongs on the top-level model. If the field is only useful
back inside the originating provider (round-trip preservation, future
push enrichment), it belongs in the opaque dict. The dict is mirrored
to `SyncMapping.provider_metadata` so a fetch-then-persist round trip
is lossless.

---

## Error mapping

`sync/errors.py` defines the hierarchy. Wrap every underlying failure
with `raise ... from exc`.

| Upstream condition                          | Wrap as                |
|---------------------------------------------|------------------------|
| HTTP 401, 403, missing/expired token        | `AuthenticationFailed` |
| HTTP 429, primary/secondary rate-limit      | `RateLimitExceeded`    |
| HTTP 5xx, network error, DNS failure        | `ProviderUnavailable`  |
| Local↔remote diverged, strategy can't reconcile | `SyncConflict`     |
| Any other provider failure                  | `SyncProviderError`    |

The CLI's batch loops catch the base `SyncProviderError` and continue
with the next task; the narrower subclasses exist for callers that want
to print a different message per failure mode (auth vs rate-limit vs
transient network).

---

## See also

- [`github-sync.md`](github-sync.md) — end-user reference for the
  bundled GitHub Issues provider.
- `sync/provider.py` — the Protocol definition with full docstrings.
- `sync/recorded.py` — `RecordedSyncProvider` test double.
- `sync/registry.py` — registry mechanics + duplicate-registration
  guard.
- `sync/errors.py` — exception hierarchy.
- `specs/2026-05-24-fakoli-state-v0.md` — canonical design spec.
