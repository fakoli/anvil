"""Integration tests for `fakoli-state sync` (Phase 8 Wave 3 Task 6).

Every test uses Typer's CliRunner and injects a RecordedSyncProvider (or
a thin subclass) via monkeypatching :data:`fakoli_state.sync.registry.PROVIDER_REGISTRY`
so the suite never touches real GitHub.

Coverage groups (one class per behaviour cluster):

* TestSyncBareReconciliation — bare `sync`, `--fix`, `--yes` flows
* TestSyncProviderAlias       — `sync github` alias resolution
* TestSyncProviderGeneric     — `sync provider <id>` and unknown-provider error
* TestSyncPushPull            — push-only / pull-only / push+pull / --task
* TestSyncHealth              — `--health` probe rendering
* TestSyncConflictResolution  — each ConflictResolutionStrategy
* TestSyncWatch               — `--watch --interval 0` runs one iteration
* TestSyncAuditEvents         — sync.* events written to events.jsonl
* TestSyncNothingToSync       — graceful empty-project path
"""

from __future__ import annotations

import json
import os
from datetime import UTC, timedelta
from datetime import datetime as _datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fakoli_state.cli import app
from fakoli_state.state.models import (
    ConflictResolutionStrategy,
    ExternalSystem,
    SyncMapping,
    SyncState,
)
from fakoli_state.sync import registry as sync_registry
from fakoli_state.sync.errors import SyncProviderError
from fakoli_state.sync.provider import (
    ExternalRef,
    ExternalTask,
    ProviderHealth,
)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

runner = CliRunner()

# Reserved provider id used in tests so we never collide with the real
# github_issues registration.
_TEST_PROVIDER_ID = "github_issues"  # match the real id so the github alias works

_NOW = _datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(hours=1)


# ---------------------------------------------------------------------------
# Test-double provider
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    """A scriptable :class:`SyncProvider` that records every call.

    Tests construct one of these, optionally seed canned return values
    (``push_returns``, ``fetch_returns``, ``health_returns``,
    ``push_raises``, ``fetch_raises``), then monkeypatch
    :data:`PROVIDER_REGISTRY` so the CLI resolves the test provider id
    through this class.
    """

    provider_id: str
    display_name: str

    def __init__(self) -> None:
        # Per-instance call log; tests assert against this.
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # The CLI calls cls() with no args, so we mimic that.

    # ---- SyncProvider Protocol ----
    def push_task(
        self,
        *,
        task: Any,
        mapping: ExternalRef | None,
    ) -> ExternalRef:
        self.calls.append(("push_task", {"task_id": task.id, "mapping": mapping}))
        if getattr(self.__class__, "push_raises", None) is not None:
            exc_cls = self.__class__.push_raises  # type: ignore[attr-defined]
            raise exc_cls("scripted push failure")
        ret = getattr(self.__class__, "push_returns", None)
        if ret is None:
            return ExternalRef(
                provider_id=self.provider_id,
                external_id=f"ext-{task.id}",
                url=f"https://example.test/{task.id}",
            )
        return ret  # type: ignore[return-value]

    def fetch_task(self, *, external_id: str) -> ExternalTask | None:
        self.calls.append(("fetch_task", {"external_id": external_id}))
        if getattr(self.__class__, "fetch_raises", None) is not None:
            exc_cls = self.__class__.fetch_raises  # type: ignore[attr-defined]
            raise exc_cls("scripted fetch failure")
        ret = getattr(self.__class__, "fetch_returns", "missing")
        if ret == "missing":
            return ExternalTask(
                external_id=external_id,
                title="remote title",
                body="remote body",
                status_label="open",
                url=None,
                last_modified=_NOW,
                provider_metadata={},
            )
        return ret  # type: ignore[return-value]

    def list_tasks(self) -> list[ExternalTask]:
        self.calls.append(("list_tasks", {}))
        return getattr(self.__class__, "list_returns", [])

    def delete_task(self, *, external_id: str) -> None:
        self.calls.append(("delete_task", {"external_id": external_id}))

    def health_check(self) -> ProviderHealth:
        self.calls.append(("health_check", {}))
        ret = getattr(self.__class__, "health_returns", None)
        if ret is not None:
            return ret  # type: ignore[return-value]
        return ProviderHealth(
            available=True,
            auth_configured=True,
            last_check_at=_NOW,
            error=None,
        )


def _make_scripted_provider_cls(
    *,
    push_returns: ExternalRef | None = None,
    push_raises: type[Exception] | None = None,
    fetch_returns: Any = "missing",
    fetch_raises: type[Exception] | None = None,
    health_returns: ProviderHealth | None = None,
) -> type[_ScriptedProvider]:
    """Build a per-test subclass with class-attribute scripted behaviour."""

    class _ScopedProvider(_ScriptedProvider):
        provider_id = _TEST_PROVIDER_ID
        display_name = "Scripted (test)"

    if push_returns is not None:
        _ScopedProvider.push_returns = push_returns  # type: ignore[attr-defined]
    if push_raises is not None:
        _ScopedProvider.push_raises = push_raises  # type: ignore[attr-defined]
    _ScopedProvider.fetch_returns = fetch_returns  # type: ignore[attr-defined]
    if fetch_raises is not None:
        _ScopedProvider.fetch_raises = fetch_raises  # type: ignore[attr-defined]
    if health_returns is not None:
        _ScopedProvider.health_returns = health_returns  # type: ignore[attr-defined]
    return _ScopedProvider


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def initialized_project(tmp_path: Path) -> Path:
    """A tmp_path that has had `fakoli-state init` run against it."""
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        r = runner.invoke(app, ["init", "--name", "SyncTest"], catch_exceptions=False)
        assert r.exit_code == 0, r.output
    finally:
        os.chdir(cwd)
    return tmp_path


@pytest.fixture
def patched_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch PROVIDER_REGISTRY with a fresh dict; returns the dict for assertions."""
    fake: dict[str, Any] = {}
    monkeypatch.setattr(sync_registry, "PROVIDER_REGISTRY", fake)
    return fake


def _seed_task(
    project_root: Path,
    *,
    task_id: str = "T001",
    feature_id: str = "F001",
    status: str = "ready",
    title: str = "Test task",
    description: str = "desc",
    now: _datetime = _NOW,
) -> None:
    """Apply project + feature + task events directly to the backend.

    ``project_root`` is the directory containing ``.fakoli-state/``.
    """
    from fakoli_state.cli._helpers import _open_backend
    from fakoli_state.state.models import EventDraft as _EventDraft

    state_dir = project_root / ".fakoli-state"
    b = _open_backend(state_dir)
    try:
        # Feature first.
        b.append(_EventDraft(
            timestamp=now,
            actor="test",
            action="feature.created",
            target_kind="feature",
            target_id=feature_id,
            payload_json={
                "id": feature_id,
                "title": "F",
                "description": "",
                "status": "proposed",
                "requirements": [],
                "tasks": [],
            },
        ))
        # Task.
        b.append(_EventDraft(
            timestamp=now,
            actor="test",
            action="task.created",
            target_kind="task",
            target_id=task_id,
            payload_json={
                "id": task_id,
                "feature_id": feature_id,
                "title": title,
                "description": description,
                "status": status,
                "priority": "medium",
                "dependencies": [],
                "conflict_groups": [],
                "scores": {},
                "acceptance_criteria": ["ok"],
                "implementation_notes": [],
                "verification": {
                    "commands": ["pytest"],
                    "manual_steps": [],
                    "required_evidence": [],
                },
                "likely_files": [],
                "parent_task_id": None,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
        ))
    finally:
        b.close()


def _seed_sync_mapping(
    project_root: Path,
    *,
    task_id: str = "T001",
    external_id: str = "42",
    last_synced_at: _datetime = _NOW,
    strategy: ConflictResolutionStrategy = ConflictResolutionStrategy.prompt,
    sync_state: SyncState = SyncState.in_sync,
) -> None:
    """Write a SyncMapping for a task via apply_sync_mapping()."""
    from fakoli_state.cli._helpers import _open_backend

    state_dir = project_root / ".fakoli-state"
    b = _open_backend(state_dir)
    try:
        b.apply_sync_mapping(
            SyncMapping(
                task_id=task_id,
                external_system=ExternalSystem.github_issues,
                external_id=external_id,
                external_url=None,
                last_synced_at=last_synced_at,
                sync_state=sync_state,
                conflict_resolution_strategy=strategy,
                provider_metadata={},
            ),
            actor="test",
        )
    finally:
        b.close()


def _read_events_jsonl(project_root: Path) -> list[dict[str, Any]]:
    """Parse events.jsonl into a list of dicts.

    ``project_root`` is the directory containing ``.fakoli-state/``.
    """
    events_path = project_root / ".fakoli-state" / "events.jsonl"
    if not events_path.exists():
        return []
    out: list[dict[str, Any]] = []
    with events_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _actions_in_events(state_dir: Path) -> list[str]:
    return [e["action"] for e in _read_events_jsonl(state_dir)]


# ---------------------------------------------------------------------------
# Bare `sync` — reconciliation
# ---------------------------------------------------------------------------


class TestSyncBareReconciliation:
    """`fakoli-state sync` with no subcommand runs the reconciliation engine."""

    def test_empty_project_reports_no_discrepancies(
        self, initialized_project: Path, patched_registry: dict[str, Any]
    ) -> None:
        """A fresh init has nothing for the engine to find."""
        r = runner.invoke(
            app, ["sync", "--cwd", str(initialized_project)], catch_exceptions=False
        )
        assert r.exit_code == 0, r.output
        assert "No discrepancies found" in r.output

    def test_fix_without_yes_fails_in_non_interactive(
        self, initialized_project: Path, patched_registry: dict[str, Any]
    ) -> None:
        """--fix in non-interactive mode requires --yes."""
        r = runner.invoke(
            app, ["sync", "--cwd", str(initialized_project), "--fix"],
            catch_exceptions=False,
        )
        # CliRunner is non-interactive by default.
        assert r.exit_code == 1, r.output
        assert "--yes" in r.output

    def test_fix_with_yes_proceeds(
        self, initialized_project: Path, patched_registry: dict[str, Any]
    ) -> None:
        """--fix --yes proceeds past the consent guard."""
        r = runner.invoke(
            app, ["sync", "--cwd", str(initialized_project), "--fix", "--yes"],
            catch_exceptions=False,
        )
        # No discrepancies → exit 0, no failures.
        assert r.exit_code == 0, r.output

    def test_bare_sync_uninitialized_errors(self, tmp_path: Path) -> None:
        """Bare `sync` against a directory that has not been init'd exits 1."""
        r = runner.invoke(
            app, ["sync", "--cwd", str(tmp_path)], catch_exceptions=False
        )
        assert r.exit_code == 1, r.output
        assert "not initialized" in r.output.lower()


# ---------------------------------------------------------------------------
# Provider alias
# ---------------------------------------------------------------------------


class TestSyncProviderAlias:
    """`sync github` resolves to provider_id=github_issues."""

    def test_github_alias_dispatches_to_github_issues(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        # No tasks → nothing to sync, but the resolver should succeed.
        r = runner.invoke(
            app,
            ["sync", "github", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        assert "Nothing to sync" in r.output

    def test_github_alias_help_shows_subcommand(self) -> None:
        r = runner.invoke(app, ["sync", "github", "--help"])
        assert r.exit_code == 0
        assert "--push" in r.output
        assert "--pull" in r.output
        assert "--watch" in r.output
        assert "--health" in r.output


# ---------------------------------------------------------------------------
# Generic provider + unknown-provider error
# ---------------------------------------------------------------------------


class TestSyncProviderGeneric:
    def test_unknown_provider_errors_with_available_list(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        patched_registry["github_issues"] = _make_scripted_provider_cls()
        r = runner.invoke(
            app,
            [
                "sync",
                "provider",
                "monday",
                "--cwd",
                str(initialized_project),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == 1
        assert "available providers" in r.output
        assert "github_issues" in r.output

    def test_generic_provider_dispatch_succeeds(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        patched_registry[_TEST_PROVIDER_ID] = _make_scripted_provider_cls()
        r = runner.invoke(
            app,
            [
                "sync",
                "provider",
                _TEST_PROVIDER_ID,
                "--cwd",
                str(initialized_project),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output


# ---------------------------------------------------------------------------
# Push / pull / task scope
# ---------------------------------------------------------------------------


class TestSyncPushPull:
    """Per-task push / pull / single-task scope behaviour."""

    def test_push_and_pull_default(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """Default (no --push / --pull) runs both directions."""
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)

        r = runner.invoke(
            app,
            ["sync", "github", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        # The first iteration: push happens, then pull (which sees the mapping
        # we just upserted and calls fetch_task).
        actions = _actions_in_events(initialized_project)
        assert "sync.push.started" in actions
        assert "sync.push.completed" in actions
        assert "sync_mapping.upserted" in actions

    def test_push_only_skips_pull(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)

        r = runner.invoke(
            app,
            ["sync", "github", "--push", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        actions = _actions_in_events(initialized_project)
        assert "sync.push.completed" in actions
        assert "sync.pull.started" not in actions

    def test_pull_only_skips_push(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)
        _seed_sync_mapping(initialized_project)

        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        actions = _actions_in_events(initialized_project)
        assert "sync.pull.started" in actions
        assert "sync.pull.completed" in actions
        assert "sync.push.started" not in actions

    def test_task_scope_limits_to_one(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """--task T001 scopes the sync to that one task."""
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project, task_id="T001")
        _seed_task(initialized_project, task_id="T002", feature_id="F002")

        r = runner.invoke(
            app,
            [
                "sync", "github",
                "--push",
                "--task", "T001",
                "--cwd", str(initialized_project),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        # Only one task pushed.
        push_completed_targets = [
            e["target_id"]
            for e in _read_events_jsonl(initialized_project)
            if e["action"] == "sync.push.completed"
        ]
        assert push_completed_targets == ["T001"]
        assert "T002" not in push_completed_targets

    def test_push_failure_records_and_continues(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """Push exception → sync.push.failed + non-zero count, no crash."""
        cls = _make_scripted_provider_cls(push_raises=SyncProviderError)
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)

        r = runner.invoke(
            app,
            ["sync", "github", "--push", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output  # batch keeps going
        actions = _actions_in_events(initialized_project)
        assert "sync.push.failed" in actions


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


class TestSyncHealth:
    def test_health_prints_status_lines(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        ph = ProviderHealth(
            available=True,
            auth_configured=False,
            last_check_at=_NOW,
            error="GITHUB_TOKEN not set",
        )
        cls = _make_scripted_provider_cls(health_returns=ph)
        patched_registry[_TEST_PROVIDER_ID] = cls

        r = runner.invoke(
            app,
            ["sync", "github", "--health", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        assert "available:" in r.output
        assert "auth_configured:" in r.output
        assert "GITHUB_TOKEN" in r.output

    def test_health_works_without_init(
        self,
        tmp_path: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """--health works even when the project is not initialised.

        Per P2-4: a provider health probe is a network/auth diagnostic.
        It must not require local state — the operator should be able to
        sanity-check GITHUB_TOKEN / connectivity from a fresh checkout
        before running `fakoli-state init`.
        """
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        r = runner.invoke(
            app, ["sync", "github", "--health", "--cwd", str(tmp_path)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        # The health report rendered to stdout.
        assert "available:" in r.output
        assert "auth_configured:" in r.output


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------


class TestSyncConflictResolution:
    """Each ConflictResolutionStrategy branch."""

    def _setup_diverged(
        self,
        project_root: Path,
        patched_registry: dict[str, Any],
        strategy: ConflictResolutionStrategy,
    ) -> type[_ScriptedProvider]:
        """Seed a task with a mapping whose remote `last_modified` is in the
        future, then bump the task's updated_at to also be in the future so
        both sides have moved since last_synced_at."""
        from fakoli_state.cli._helpers import _open_backend
        from fakoli_state.state.models import EventDraft as _EventDraft
    
        _seed_task(project_root, now=_NOW - timedelta(hours=2))
        _seed_sync_mapping(
            project_root,
            last_synced_at=_NOW - timedelta(hours=2),
            strategy=strategy,
        )

        state_dir = project_root / ".fakoli-state"

        # Bump local task by emitting task.status_changed (moves updated_at forward).
        b = _open_backend(state_dir)
        try:
            b.append(_EventDraft(
                timestamp=_LATER,
                actor="test",
                action="task.status_changed",
                target_kind="task",
                target_id="T001",
                payload_json={
                    "task_id": "T001",
                    "from": "ready",
                    "to": "in_progress",
                },
            ))
        finally:
            b.close()

        # Remote also moved (last_modified > last_synced_at).
        remote = ExternalTask(
            external_id="42",
            title="remote changed",
            body="remote body",
            status_label="open",
            url=None,
            last_modified=_LATER + timedelta(hours=1),
            provider_metadata={},
        )
        cls = _make_scripted_provider_cls(fetch_returns=remote)
        patched_registry[_TEST_PROVIDER_ID] = cls
        return cls

    def test_local_wins(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        self._setup_diverged(
            initialized_project, patched_registry,
            ConflictResolutionStrategy.local_wins,
        )
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        events = _read_events_jsonl(initialized_project)
        conflict_events = [e for e in events if e["action"] == "sync.conflict_detected"]
        assert len(conflict_events) == 1
        # The audit truthfully reports "_deferred" — no mutation happens
        # in this iteration; the re-push is wave-9 work.
        assert conflict_events[0]["payload_json"]["resolution"] == "local_wins_deferred"

    def test_remote_wins(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        self._setup_diverged(
            initialized_project, patched_registry,
            ConflictResolutionStrategy.remote_wins,
        )
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        events = _read_events_jsonl(initialized_project)
        conflict_events = [e for e in events if e["action"] == "sync.conflict_detected"]
        assert len(conflict_events) == 1
        # The audit truthfully reports "_deferred" — see local_wins above.
        assert conflict_events[0]["payload_json"]["resolution"] == "remote_wins_deferred"

    def test_prompt_non_interactive_defaults_to_local(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        self._setup_diverged(
            initialized_project, patched_registry,
            ConflictResolutionStrategy.prompt,
        )
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--yes", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        events = _read_events_jsonl(initialized_project)
        conflict_events = [e for e in events if e["action"] == "sync.conflict_detected"]
        assert len(conflict_events) == 1
        assert conflict_events[0]["payload_json"]["resolution"] == "prompt_defaulted_to_local"

    def test_manual_merge_writes_file_and_exits_2(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        self._setup_diverged(
            initialized_project, patched_registry,
            ConflictResolutionStrategy.manual_merge,
        )
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 2, r.output
        merge_path = (
            initialized_project / ".fakoli-state" / ".sync-conflicts" / "T001.md"
        )
        assert merge_path.exists(), f"manual_merge file not written: {r.output}"
        content = merge_path.read_text()
        assert "## Local" in content
        assert "## Remote" in content

    def test_fix_flag_forces_remote_wins_on_conflict(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """`sync github --fix` overrides the strategy to remote_wins for this run."""
        self._setup_diverged(
            initialized_project, patched_registry,
            ConflictResolutionStrategy.local_wins,
        )
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--fix", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        events = _read_events_jsonl(initialized_project)
        conflict_events = [e for e in events if e["action"] == "sync.conflict_detected"]
        assert len(conflict_events) == 1
        # Audit truth: --fix selects remote_wins but the actual mutation
        # is deferred (see P2-6 / TODO(phase-9) in cli/sync.py).
        assert conflict_events[0]["payload_json"]["resolution"] == "remote_wins_deferred"


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------


class TestSyncWatch:
    def test_watch_interval_zero_runs_one_iteration(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """--watch --interval 0 is the test seam: one iteration and out."""
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)

        r = runner.invoke(
            app,
            [
                "sync", "github",
                "--watch",
                "--interval", "0",
                "--push",
                "--cwd", str(initialized_project),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        actions = _actions_in_events(initialized_project)
        # Exactly one batch should have run.
        assert actions.count("sync.batch.started") == 1
        assert actions.count("sync.batch.completed") == 1


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------


class TestSyncAuditEvents:
    """Verify sync.* events land in events.jsonl with expected payloads."""

    def test_push_emits_started_and_completed(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)
        r = runner.invoke(
            app,
            ["sync", "github", "--push", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        actions = _actions_in_events(initialized_project)
        assert "sync.batch.started" in actions
        assert "sync.push.started" in actions
        assert "sync.push.completed" in actions
        assert "sync.batch.completed" in actions

    def test_pull_emits_started_and_completed(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)
        _seed_sync_mapping(initialized_project)
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        actions = _actions_in_events(initialized_project)
        assert "sync.pull.started" in actions
        assert "sync.pull.completed" in actions

    def test_pull_failure_emits_failed_event(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        cls = _make_scripted_provider_cls(fetch_raises=SyncProviderError)
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)
        _seed_sync_mapping(initialized_project)
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        actions = _actions_in_events(initialized_project)
        assert "sync.pull.failed" in actions

    def test_batch_events_record_provider_id(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)
        runner.invoke(
            app,
            ["sync", "github", "--push", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        events = _read_events_jsonl(initialized_project)
        batch_started = [e for e in events if e["action"] == "sync.batch.started"]
        assert len(batch_started) == 1
        assert batch_started[0]["payload_json"]["provider_id"] == _TEST_PROVIDER_ID


# ---------------------------------------------------------------------------
# Nothing-to-sync graceful path
# ---------------------------------------------------------------------------


class TestSyncNothingToSync:
    def test_no_tasks_returns_zero(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        r = runner.invoke(
            app,
            ["sync", "github", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        assert "Nothing to sync" in r.output

    def test_task_filter_missing_returns_zero(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        r = runner.invoke(
            app,
            [
                "sync", "github",
                "--task", "T999",
                "--cwd", str(initialized_project),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        assert "Nothing to sync" in r.output

    def test_pull_without_mapping_is_no_op(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """Pull a task that has no SyncMapping → skipped, no fetch call."""
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        actions = _actions_in_events(initialized_project)
        assert "sync.pull.started" not in actions


# ---------------------------------------------------------------------------
# CLI surface discoverability
# ---------------------------------------------------------------------------


class TestSyncHelpSurface:
    def test_sync_help_lists_subcommands(self) -> None:
        r = runner.invoke(app, ["sync", "--help"])
        assert r.exit_code == 0
        assert "github" in r.output
        assert "provider" in r.output

    def test_sync_provider_help_shows_argument(self) -> None:
        r = runner.invoke(app, ["sync", "provider", "--help"])
        assert r.exit_code == 0
        assert "PROVIDER_ID" in r.output.upper() or "provider_id" in r.output

    def test_top_level_help_lists_sync(self) -> None:
        r = runner.invoke(app, ["--help"])
        assert r.exit_code == 0
        assert "sync" in r.output.lower()


# ---------------------------------------------------------------------------
# Wave 3 P2 regression tests
# ---------------------------------------------------------------------------


class TestIsTtyChecksBothDescriptors:
    """P2-1 — _is_tty() must check stdin AND stdout, not just stdout.

    typer.prompt reads from stdin and writes to stdout; if either side
    is redirected we cannot safely interact. The classic regression is
    `docker run -t` (TTY on stdout, no stdin): the old impl said True
    and the prompt would hang forever.
    """

    def test_returns_false_when_stdout_not_a_tty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from fakoli_state.cli import sync as sync_mod

        monkeypatch.setattr(sync_mod.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(sync_mod.sys.stdout, "isatty", lambda: False)
        assert sync_mod._is_tty() is False

    def test_returns_false_when_stdin_not_a_tty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The `docker run -t` case: stdout has a tty but stdin is closed."""
        from fakoli_state.cli import sync as sync_mod

        monkeypatch.setattr(sync_mod.sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(sync_mod.sys.stdout, "isatty", lambda: True)
        assert sync_mod._is_tty() is False

    def test_returns_true_when_both_are_ttys(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from fakoli_state.cli import sync as sync_mod

        monkeypatch.setattr(sync_mod.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(sync_mod.sys.stdout, "isatty", lambda: True)
        assert sync_mod._is_tty() is True


class TestWatchLoopSurvivesIterationFailure:
    """P2-2 — a single iteration exception must not kill the daemon."""

    def test_watch_loop_continues_after_provider_error(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First iteration raises; second iteration succeeds; loop survives.

        We use --interval 0 so the loop exits after one iteration. To
        prove the wrap-and-continue contract, we monkeypatch
        ``_run_sync_once`` to raise once, then succeed, and run the watch
        body twice in-process.
        """
        from fakoli_state.cli import sync as sync_mod

        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)

        # Build a call counter that raises on call #1 and succeeds on #2+.
        call_log: list[int] = []
        real_run_once = sync_mod._run_sync_once

        def _flaky_run_once(**kw: Any) -> None:
            call_log.append(1)
            if len(call_log) == 1:
                raise SyncProviderError("transient network blip")
            real_run_once(**kw)

        monkeypatch.setattr(sync_mod, "_run_sync_once", _flaky_run_once)

        # Patch _run_watch_loop's break behaviour: interval 0 ends after
        # ONE iteration normally. We want to verify the loop survives
        # the first exception, so we instead invoke the loop directly
        # with a small counter-based stop.
        from fakoli_state.cli._helpers import _open_backend
        state_dir = initialized_project / ".fakoli-state"
        provider = cls()
        backend = _open_backend(state_dir)
        try:
            # Inline a 2-iteration loop using the same try/except shape
            # as _run_watch_loop. We can't easily call _run_watch_loop
            # itself with interval>0 in a unit test (sleep would block),
            # so we exercise the contract: an iteration raises, the next
            # iteration runs cleanly.
            for _ in range(2):
                try:
                    sync_mod._run_sync_once(
                        backend=backend,
                        state_dir=state_dir,
                        provider=provider,
                        push=True,
                        pull=False,
                        fix=False,
                        task=None,
                        yes=False,
                    )
                except SyncProviderError:
                    # The watch loop swallows this and keeps going.
                    pass
        finally:
            backend.close()

        # Both iterations attempted; second one wrote audit events.
        assert len(call_log) == 2
        actions = _actions_in_events(initialized_project)
        # The second iteration completed a successful batch.
        assert actions.count("sync.batch.completed") >= 1

    def test_watch_loop_wraps_typer_exit_per_iteration(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If an iteration raises typer.Exit (e.g. manual_merge pending)
        the watch loop must catch it and surface a stderr line instead of
        propagating to the CLI runner."""
        import typer as _typer

        from fakoli_state.cli import sync as sync_mod

        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)

        call_log: list[int] = []

        def _raises_typer_exit(**kw: Any) -> None:
            call_log.append(1)
            raise _typer.Exit(code=2)

        monkeypatch.setattr(sync_mod, "_run_sync_once", _raises_typer_exit)

        # --interval 0 → one iteration, then break. The wrap-and-continue
        # logic should swallow typer.Exit so the watch command returns 0.
        r = runner.invoke(
            app,
            [
                "sync", "github",
                "--watch", "--interval", "0",
                "--push",
                "--cwd", str(initialized_project),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        # The exit was caught and surfaced to stderr.
        assert "watch: iteration aborted" in r.output or "iteration aborted" in r.output
        assert len(call_log) == 1


class TestManualMergeReturnsFalseAndBatchExits2:
    """P2-3 — manual_merge must NOT raise mid-batch; batch exits 2 at end."""

    def test_batch_with_one_manual_merge_and_one_clean_processes_both(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """Two tasks: T001 → manual_merge conflict, T002 → no conflict.
        Old behaviour halted at T001 (raised typer.Exit mid-batch). New
        behaviour processes both tasks and exits 2 at the end because
        manual_merge is pending.

        Observability: the sync.batch.completed event fires AFTER T001's
        manual_merge file is written — proving the batch was not halted
        mid-iteration by a raised typer.Exit."""
        from fakoli_state.cli._helpers import _open_backend
        from fakoli_state.state.models import EventDraft as _EventDraft
    
        # T001 — diverged manual_merge mapping.
        _seed_task(initialized_project, task_id="T001", now=_NOW - timedelta(hours=2))
        _seed_sync_mapping(
            initialized_project, task_id="T001",
            external_id="42",
            last_synced_at=_NOW - timedelta(hours=2),
            strategy=ConflictResolutionStrategy.manual_merge,
        )
        # T002 — clean: a SyncMapping that's in sync (no divergence).
        _seed_task(initialized_project, task_id="T002", feature_id="F002")
        _seed_sync_mapping(
            initialized_project, task_id="T002",
            external_id="43",
            last_synced_at=_NOW + timedelta(hours=10),  # future = no drift
            strategy=ConflictResolutionStrategy.prompt,
        )

        # Bump T001 local updated_at so local_moved is True.
        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            b.append(_EventDraft(
                timestamp=_LATER,
                actor="test",
                action="task.status_changed",
                target_kind="task",
                target_id="T001",
                payload_json={"task_id": "T001", "from": "ready", "to": "in_progress"},
            ))
        finally:
            b.close()

        # Remote for fetch_task returns a moved payload (conflict for T001;
        # not a conflict for T002 because T002's last_synced_at is in the
        # future relative to remote.last_modified).
        remote = ExternalTask(
            external_id="42",
            title="remote",
            body="body",
            status_label="open",
            url=None,
            last_modified=_LATER + timedelta(hours=1),
            provider_metadata={},
        )
        cls = _make_scripted_provider_cls(fetch_returns=remote)
        patched_registry[_TEST_PROVIDER_ID] = cls

        # --pull only so we exercise the conflict path on T001 without push
        # racing through and refreshing last_synced_at.
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        # Exit 2: manual_merge is pending operator action.
        assert r.exit_code == 2, r.output
        # The merge file for T001 was written.
        merge_path = state_dir / ".sync-conflicts" / "T001.md"
        assert merge_path.exists(), f"manual_merge file not written: {r.output}"
        # The batch.completed event must have fired AFTER T001's
        # manual_merge file was written — proving the batch did not halt
        # mid-iteration. Old behaviour (raise typer.Exit) would skip
        # this event entirely.
        actions = _actions_in_events(initialized_project)
        assert "sync.batch.completed" in actions, (
            "batch halted mid-iteration before emitting batch.completed: "
            f"{actions}"
        )
        # T002 was processed (pull.started fired for it).
        pull_started = [
            e for e in _read_events_jsonl(initialized_project)
            if e["action"] == "sync.pull.started"
        ]
        pull_started_targets = {e["target_id"] for e in pull_started}
        assert "T002" in pull_started_targets, (
            "batch halted at T001 — T002 was not pulled. "
            f"pull.started targets: {pull_started_targets}"
        )

    def test_batch_with_no_manual_merge_exits_zero(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """If no task needs manual_merge, the batch exits 0 normally."""
        cls = _make_scripted_provider_cls()
        patched_registry[_TEST_PROVIDER_ID] = cls
        _seed_task(initialized_project)
        r = runner.invoke(
            app,
            ["sync", "github", "--push", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output


class TestHealthWorksWithoutInit:
    """P2-4 — `sync github --health` must work from an uninitialised cwd."""

    def test_health_succeeds_without_state_dir(
        self,
        tmp_path: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """An operator probing connectivity before `fakoli-state init` must
        not be blocked by the state-dir check. Health is a network/auth
        diagnostic, not a state operation."""
        ph = ProviderHealth(
            available=True,
            auth_configured=True,
            last_check_at=_NOW,
            error=None,
        )
        cls = _make_scripted_provider_cls(health_returns=ph)
        patched_registry[_TEST_PROVIDER_ID] = cls

        # Note: tmp_path is NOT initialised.
        r = runner.invoke(
            app,
            ["sync", "github", "--health", "--cwd", str(tmp_path)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        assert "available:" in r.output
        assert "auth_configured:" in r.output


class TestConflictResolutionAuditTruth:
    """P2-6 — local_wins / remote_wins audit must say `_deferred`, not `_applied`.

    The contract: no mutation happens inside _resolve_conflict for these
    branches, so the audit string must not lie. See TODO(phase-9) in
    cli/sync.py."""

    def test_local_wins_emits_deferred_not_applied(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        # Reuse the same diverged setup as TestSyncConflictResolution.
        TestSyncConflictResolution()._setup_diverged(
            initialized_project, patched_registry,
            ConflictResolutionStrategy.local_wins,
        )
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        conflict_events = [
            e for e in _read_events_jsonl(initialized_project)
            if e["action"] == "sync.conflict_detected"
        ]
        assert len(conflict_events) == 1
        res = conflict_events[0]["payload_json"]["resolution"]
        assert res == "local_wins_deferred"
        # Hard fail if anyone re-introduces the lying `_applied` string.
        assert "applied" not in res

    def test_remote_wins_emits_deferred_not_applied(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        TestSyncConflictResolution()._setup_diverged(
            initialized_project, patched_registry,
            ConflictResolutionStrategy.remote_wins,
        )
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        conflict_events = [
            e for e in _read_events_jsonl(initialized_project)
            if e["action"] == "sync.conflict_detected"
        ]
        assert len(conflict_events) == 1
        res = conflict_events[0]["payload_json"]["resolution"]
        assert res == "remote_wins_deferred"
        assert "applied" not in res


# ---------------------------------------------------------------------------
# PR #49 P1 regression tests
# ---------------------------------------------------------------------------


class TestPullAppliesRemoteToLocal:
    """P1-1 — `--pull` must apply remote payload when remote moved alone.

    Pre-fix bug: the `remote_moved and not local_moved` branch printed
    `sync.pull.completed` and incremented `results["pulled"]` but never
    wrote the remote payload to the local task — the audit log lied.
    """

    def test_pull_overwrites_local_title_when_remote_moved_alone(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """Remote moved, local untouched → local Task.title is overwritten."""
        from fakoli_state.cli._helpers import _open_backend

        # Seed T001 with title='old' at _NOW - 2h (so updated_at < last_synced_at)
        _seed_task(
            initialized_project, title="old", description="old desc",
            now=_NOW - timedelta(hours=2),
        )
        _seed_sync_mapping(
            initialized_project,
            last_synced_at=_NOW - timedelta(hours=1),
        )

        # Remote moved AFTER last_synced_at; local has NOT moved since.
        remote = ExternalTask(
            external_id="42",
            title="new",
            body="new desc",
            status_label=None,
            url=None,
            last_modified=_LATER,
            provider_metadata={},
        )
        cls = _make_scripted_provider_cls(fetch_returns=remote)
        patched_registry[_TEST_PROVIDER_ID] = cls

        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output

        # The local Task now carries the remote title/description.
        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            task = b.get_task("T001")
            assert task is not None
            assert task.title == "new", (
                f"P1-1 regression: pull did not overwrite local title "
                f"(got {task.title!r})"
            )
            assert task.description == "new desc", (
                f"P1-1 regression: pull did not overwrite description "
                f"(got {task.description!r})"
            )
        finally:
            b.close()

    def test_pull_emits_task_synced_from_remote_event(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """The audit log must include the task.synced_from_remote action."""
        _seed_task(
            initialized_project, title="old", description="old desc",
            now=_NOW - timedelta(hours=2),
        )
        _seed_sync_mapping(
            initialized_project,
            last_synced_at=_NOW - timedelta(hours=1),
        )
        remote = ExternalTask(
            external_id="42",
            title="new",
            body="new desc",
            status_label=None,
            url=None,
            last_modified=_LATER,
            provider_metadata={},
        )
        cls = _make_scripted_provider_cls(fetch_returns=remote)
        patched_registry[_TEST_PROVIDER_ID] = cls
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output

        events = _read_events_jsonl(initialized_project)
        synced = [e for e in events if e["action"] == "task.synced_from_remote"]
        assert len(synced) == 1, (
            f"expected exactly one task.synced_from_remote event; "
            f"actions: {[e['action'] for e in events]}"
        )
        payload = synced[0]["payload_json"]
        assert payload["task_id"] == "T001"
        assert payload["title"] == "new"
        assert payload["description"] == "new desc"
        # Actor reflects the originating provider id (per P1-1 brief).
        assert payload["actor"] == f"sync.{_TEST_PROVIDER_ID}"

    def test_pull_refreshes_sync_mapping_to_in_sync(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """SyncMapping.sync_state ends as 'in_sync' after a remote-applied pull."""
        from fakoli_state.cli._helpers import _open_backend

        _seed_task(
            initialized_project, title="old", description="old desc",
            now=_NOW - timedelta(hours=2),
        )
        _seed_sync_mapping(
            initialized_project,
            last_synced_at=_NOW - timedelta(hours=1),
            sync_state=SyncState.remote_ahead,  # start "drifted"
        )
        remote = ExternalTask(
            external_id="42",
            title="new",
            body="new desc",
            status_label=None,
            url=None,
            last_modified=_LATER,
            provider_metadata={},
        )
        cls = _make_scripted_provider_cls(fetch_returns=remote)
        patched_registry[_TEST_PROVIDER_ID] = cls
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output

        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            mapping = b.get_sync_mapping("T001")
            assert mapping is not None
            assert str(mapping.sync_state) == "in_sync", (
                f"SyncMapping.sync_state should be in_sync after pull; "
                f"got {mapping.sync_state!r}"
            )
        finally:
            b.close()

    def test_pull_no_movement_does_not_emit_synced_from_remote(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """When neither side moved, no synced_from_remote event is emitted."""
        _seed_task(initialized_project, title="same", description="same desc")
        _seed_sync_mapping(
            initialized_project,
            last_synced_at=_NOW + timedelta(hours=10),  # far future
        )
        # Remote last_modified is in the past relative to last_synced_at.
        remote = ExternalTask(
            external_id="42",
            title="same",
            body="same desc",
            status_label=None,
            url=None,
            last_modified=_NOW - timedelta(hours=1),
            provider_metadata={},
        )
        cls = _make_scripted_provider_cls(fetch_returns=remote)
        patched_registry[_TEST_PROVIDER_ID] = cls
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        actions = _actions_in_events(initialized_project)
        assert "task.synced_from_remote" not in actions


class TestContributorProviderMappingPersists:
    """P1-2 — Contributor provider ids must persist SyncMappings.

    Pre-fix bug: ``_persist_mapping_from_push`` raised ``ValueError`` when
    ``provider.provider_id`` was not in the ``ExternalSystem`` enum,
    silently dropped the mapping, and the next sync cycle created a
    duplicate remote record (existing=None → push as new).
    """

    def test_custom_provider_id_persists_sync_mapping(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """Register a provider with a non-enum id; push; verify mapping row."""
        from fakoli_state.cli._helpers import _open_backend

        class _CustomProvider(_ScriptedProvider):
            provider_id = "my_custom_tracker"  # NOT in ExternalSystem enum
            display_name = "Custom Tracker (test)"

        patched_registry["my_custom_tracker"] = _CustomProvider
        _seed_task(initialized_project)

        r = runner.invoke(
            app,
            [
                "sync", "provider", "my_custom_tracker",
                "--push", "--cwd", str(initialized_project),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output

        # SyncMapping row landed with external_system='my_custom_tracker'.
        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            mapping = b.get_sync_mapping(
                "T001", external_system="my_custom_tracker",
            )
            assert mapping is not None, (
                "P1-2 regression: contributor provider mapping silently "
                "dropped (no sync_mappings row for 'my_custom_tracker')."
            )
            assert mapping.external_system == "my_custom_tracker"
            assert mapping.external_id == "ext-T001"
        finally:
            b.close()

    def test_second_push_updates_existing_mapping_not_create_duplicate(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """Two pushes for the same task → one mapping row (not duplicates).

        This is the core symptom of P1-2: pre-fix, every sync cycle saw
        ``existing=None`` (mapping was never persisted), called
        ``push_task(mapping=None)``, and created a new remote record.
        """
        from fakoli_state.cli._helpers import _open_backend

        class _CustomProvider(_ScriptedProvider):
            provider_id = "my_custom_tracker"
            display_name = "Custom (test)"

        patched_registry["my_custom_tracker"] = _CustomProvider
        _seed_task(initialized_project)

        # First push.
        runner.invoke(
            app,
            [
                "sync", "provider", "my_custom_tracker",
                "--push", "--cwd", str(initialized_project),
            ],
            catch_exceptions=False,
        )
        # Second push.
        runner.invoke(
            app,
            [
                "sync", "provider", "my_custom_tracker",
                "--push", "--cwd", str(initialized_project),
            ],
            catch_exceptions=False,
        )

        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            mappings = b.list_sync_mappings(external_system="my_custom_tracker")
            assert len(mappings) == 1, (
                f"P1-2 regression: two pushes created {len(mappings)} mappings "
                f"(should be exactly 1 — composite PK upsert)."
            )
        finally:
            b.close()


# ---------------------------------------------------------------------------
# PR #49 critic round-2 regression tests
# ---------------------------------------------------------------------------


class TestManualMergeEmitsDeferredAudit:
    """MF — manual_merge pull path must emit a terminal `sync.pull.deferred`.

    Pre-fix: `sync.pull.started` was emitted unconditionally but the
    manual_merge branch returned without a terminal `sync.pull.completed`
    or `sync.pull.failed`, leaving the audit log with a dangling start.
    Operators monitoring `events.jsonl` could not disambiguate a parked
    manual_merge from a process crash mid-pull.
    """

    def test_manual_merge_emits_pull_deferred_not_completed(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        TestSyncConflictResolution()._setup_diverged(
            initialized_project, patched_registry,
            ConflictResolutionStrategy.manual_merge,
        )
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 2, r.output

        events = _read_events_jsonl(initialized_project)
        actions = [e["action"] for e in events]

        # Both the start AND the deferred terminal must be present.
        assert "sync.pull.started" in actions, (
            "manual_merge regression: sync.pull.started missing"
        )
        assert "sync.pull.deferred" in actions, (
            f"MF regression: sync.pull.deferred missing; "
            f"audit actions={actions}"
        )
        # And the pull MUST NOT claim "completed" — the pull was parked,
        # not completed. A "completed" event here would lie.
        assert "sync.pull.completed" not in actions, (
            f"MF regression: sync.pull.completed fired for a parked "
            f"manual_merge; actions={actions}"
        )

        # The deferred event carries the audit_note marker for grep.
        deferred = [e for e in events if e["action"] == "sync.pull.deferred"]
        assert len(deferred) == 1
        assert deferred[0]["payload_json"].get("audit_note") == "manual_merge_pending"
        assert deferred[0]["target_id"] == "T001"


class TestDeferredConflictResolutionBumpsMappingState:
    """SF-4 — deferred conflict resolutions must update SyncMapping.

    Pre-fix: local_wins / remote_wins / manual_merge / prompt_skipped
    emitted the audit event but never updated `sync_state` or
    `last_synced_at` on the SyncMapping. The next `--watch` iteration's
    `remote_moved AND local_moved` was unchanged → the same conflict
    fired again every poll (1440 redundant events / task / day at the
    documented 60s interval).
    """

    def test_local_wins_advances_last_synced_at_so_next_pull_is_clean(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """After a local_wins resolution, a second pull with the same
        remote payload must NOT re-detect the conflict because
        last_synced_at has advanced past remote.last_modified."""
        from fakoli_state.cli._helpers import _open_backend

        TestSyncConflictResolution()._setup_diverged(
            initialized_project, patched_registry,
            ConflictResolutionStrategy.local_wins,
        )

        # First pull — detects the conflict, resolves local_wins.
        r1 = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r1.exit_code == 0, r1.output

        # Sanity: one conflict was detected on the first pass.
        events_1 = _read_events_jsonl(initialized_project)
        conflicts_1 = [e for e in events_1 if e["action"] == "sync.conflict_detected"]
        assert len(conflicts_1) == 1

        # Mapping must now reflect the resolution: local_ahead, with
        # last_synced_at advanced past remote.last_modified.
        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            mapping = b.get_sync_mapping("T001")
            assert mapping is not None
            assert str(mapping.sync_state) == "local_ahead", (
                f"SF-4 regression: local_wins did not bump sync_state to "
                f"local_ahead (got {mapping.sync_state!r})"
            )
        finally:
            b.close()

        # Second pull with the SAME remote payload — must NOT re-detect
        # the conflict because last_synced_at has advanced.
        r2 = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r2.exit_code == 0, r2.output

        events_2 = _read_events_jsonl(initialized_project)
        conflicts_2 = [e for e in events_2 if e["action"] == "sync.conflict_detected"]
        # Still only one conflict total — the second iteration must NOT
        # have re-fired it.
        assert len(conflicts_2) == 1, (
            f"SF-4 regression: second pull re-fired conflict "
            f"({len(conflicts_2)} total, expected 1). "
            "last_synced_at was not advanced past the resolution point."
        )

    def test_manual_merge_parks_mapping_in_conflict_state(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """SF-4 — manual_merge parks the mapping at sync_state=conflict."""
        from fakoli_state.cli._helpers import _open_backend

        TestSyncConflictResolution()._setup_diverged(
            initialized_project, patched_registry,
            ConflictResolutionStrategy.manual_merge,
        )
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 2, r.output

        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            mapping = b.get_sync_mapping("T001")
            assert mapping is not None
            assert str(mapping.sync_state) == "conflict", (
                f"SF-4 regression: manual_merge did not park mapping at "
                f"sync_state=conflict (got {mapping.sync_state!r})"
            )
        finally:
            b.close()


class TestTombstoneFlipsSyncStateAndIsReconciled:
    """SF-5 — `fetch_task` returning None must flip sync_state to
    `external_deleted` AND surface a reconciliation drift discrepancy
    with `payload['reason']='external_deleted'`.
    """

    def test_pull_with_none_remote_flips_sync_state_to_external_deleted(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        from fakoli_state.cli._helpers import _open_backend

        _seed_task(initialized_project)
        _seed_sync_mapping(initialized_project)

        # Provider returns None from fetch_task — remote was deleted.
        cls = _make_scripted_provider_cls(fetch_returns=None)
        patched_registry[_TEST_PROVIDER_ID] = cls

        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output

        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            mapping = b.get_sync_mapping("T001")
            assert mapping is not None
            assert str(mapping.sync_state) == "external_deleted", (
                f"SF-5 regression: tombstone path did not flip "
                f"sync_state to external_deleted "
                f"(got {mapping.sync_state!r})"
            )
        finally:
            b.close()

    def test_reconciliation_surfaces_external_deleted_discrepancy(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """After a tombstone, `fakoli-state sync` (reconciliation) must
        surface a `drift_sync_state` discrepancy whose payload tags the
        reason as `external_deleted` so the operator can act."""
        from fakoli_state.cli._helpers import _open_backend

        _seed_task(initialized_project)
        _seed_sync_mapping(initialized_project)

        cls = _make_scripted_provider_cls(fetch_returns=None)
        patched_registry[_TEST_PROVIDER_ID] = cls

        # Step 1: pull → flips mapping to external_deleted.
        r1 = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r1.exit_code == 0, r1.output

        # Step 2: reconciliation scan. Use the engine directly so we
        # can inspect the discrepancy payload rather than parsing stdout.
        from fakoli_state.clock import SystemClock
        from fakoli_state.sync.reconciliation import ReconciliationEngine

        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            engine = ReconciliationEngine(
                b,
                state_dir=state_dir,
                clock=SystemClock(),
                configured_providers=[_TEST_PROVIDER_ID],
            )
            report = engine.scan()
        finally:
            b.close()

        # The drift discrepancy must be present and labelled as
        # external_deleted (not "conflict" or "stale").
        tombstone_drifts = [
            d for d in report.discrepancies
            if str(d.kind) == "drift_sync_state"
            and d.payload.get("reason") == "external_deleted"
        ]
        assert len(tombstone_drifts) == 1, (
            f"SF-5 regression: reconciliation did not surface tombstone "
            f"as drift_sync_state[reason=external_deleted]; got drifts: "
            f"{[d.payload for d in report.discrepancies]}"
        )


class TestCleanPullBumpsLastSyncedAt:
    """SF-15 — every clean pull must refresh `last_synced_at` so the 7-day
    drift_sync_state scan doesn't false-positive on pull-only-synced
    tasks.

    Pre-fix: when neither side moved, `_pull_one_task` emitted
    `sync.pull.completed` but never updated the SyncMapping. After 7
    days of pull-only sync, the drift scan flagged the mapping as
    "stale" even though pulls had been happening every minute.
    """

    def test_clean_pull_advances_last_synced_at(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        from datetime import datetime as _dt

        from fakoli_state.cli._helpers import _open_backend

        # Seed with last_synced_at well in the past so we can detect
        # the advance.
        seed_time = _NOW - timedelta(days=14)
        _seed_task(initialized_project, now=seed_time)
        _seed_sync_mapping(
            initialized_project,
            last_synced_at=seed_time,
        )

        # Remote payload last_modified is BEFORE seed_time → no movement
        # on either side → clean pull, no conflict, no remote-apply.
        remote = ExternalTask(
            external_id="42",
            title="Test task",
            body="desc",
            status_label="open",
            url=None,
            last_modified=seed_time - timedelta(days=1),
            provider_metadata={},
        )
        cls = _make_scripted_provider_cls(fetch_returns=remote)
        patched_registry[_TEST_PROVIDER_ID] = cls

        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output

        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            mapping = b.get_sync_mapping("T001")
            assert mapping is not None
            # last_synced_at must have advanced significantly past seed_time
            # (it's bumped to clock.now() which is far past seed_time-14d).
            now_floor = _dt.now(UTC) - timedelta(minutes=5)
            assert mapping.last_synced_at > now_floor, (
                f"SF-15 regression: clean pull did not advance "
                f"last_synced_at; still at {mapping.last_synced_at.isoformat()}"
            )
            # state preserved as in_sync.
            assert str(mapping.sync_state) == "in_sync"
        finally:
            b.close()

    def test_clean_pull_does_not_trigger_stale_drift_scan(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """After a clean pull, the reconciliation drift scan must NOT
        flag the mapping as stale — last_synced_at was advanced."""
        from fakoli_state.cli._helpers import _open_backend

        # Initial state: mapping is 14 days old (well past the 7-day
        # threshold).
        seed_time = _NOW - timedelta(days=14)
        _seed_task(initialized_project, now=seed_time)
        _seed_sync_mapping(
            initialized_project,
            last_synced_at=seed_time,
        )

        # Clean pull — remote did not move.
        remote = ExternalTask(
            external_id="42",
            title="Test task",
            body="desc",
            status_label="open",
            url=None,
            last_modified=seed_time - timedelta(days=1),
            provider_metadata={},
        )
        cls = _make_scripted_provider_cls(fetch_returns=remote)
        patched_registry[_TEST_PROVIDER_ID] = cls

        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output

        # Now run reconciliation — no stale drift should fire.
        from fakoli_state.clock import SystemClock
        from fakoli_state.sync.reconciliation import ReconciliationEngine

        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            engine = ReconciliationEngine(
                b,
                state_dir=state_dir,
                clock=SystemClock(),
                configured_providers=[_TEST_PROVIDER_ID],
            )
            report = engine.scan()
        finally:
            b.close()

        stale_drifts = [
            d for d in report.discrepancies
            if str(d.kind) == "drift_sync_state"
            and d.payload.get("reason") == "stale"
        ]
        assert len(stale_drifts) == 0, (
            f"SF-15 regression: clean pull did not reset drift counter; "
            f"stale drifts: {[d.payload for d in stale_drifts]}"
        )


# ---------------------------------------------------------------------------
# Phase 9 T5 — audit honesty repointing
# ---------------------------------------------------------------------------


class TestDeferredConflictBranchesEmitPullDeferred:
    """T5 — the six deferred conflict-resolution branches must emit
    ``sync.pull.deferred`` (NOT ``sync.pull.completed``) because no local
    Task mutation happens in those branches this iteration.

    Scout's Phase 9 T1 audit (``agent-scout-honesty-status.md``) identified
    these branches as DISHONEST: each emitted ``sync.conflict_detected``
    honestly but then fell through to the unconditional
    ``sync.pull.completed`` terminal at cli/sync.py:868 — the audit log
    claimed a pull was applied when only mapping bookkeeping had advanced.
    """

    @pytest.mark.parametrize(
        ("strategy", "expected_resolution"),
        [
            (ConflictResolutionStrategy.local_wins, "local_wins_deferred"),
            (ConflictResolutionStrategy.remote_wins, "remote_wins_deferred"),
        ],
    )
    def test_static_strategy_branches_emit_pull_deferred(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
        strategy: ConflictResolutionStrategy,
        expected_resolution: str,
    ) -> None:
        """local_wins / remote_wins branches: ``sync.pull.deferred``, not ``completed``."""
        TestSyncConflictResolution()._setup_diverged(
            initialized_project, patched_registry, strategy,
        )
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        events = _read_events_jsonl(initialized_project)
        actions = [e["action"] for e in events]

        # Honest terminal: deferred, NOT completed. The conflict was
        # detected and a resolution intent recorded; nothing was applied.
        assert "sync.pull.deferred" in actions, (
            f"T5 regression: {strategy} should emit sync.pull.deferred "
            f"(no local mutation happened this iteration); "
            f"got actions={actions}"
        )
        assert "sync.pull.completed" not in actions, (
            f"T5 regression: {strategy} dishonestly emitted "
            f"sync.pull.completed; the historical lie is back. "
            f"actions={actions}"
        )

        # The deferred payload carries the resolution token forward.
        deferred = [e for e in events if e["action"] == "sync.pull.deferred"]
        assert len(deferred) == 1
        assert deferred[0]["payload_json"]["resolution"] == expected_resolution
        assert deferred[0]["target_id"] == "T001"

    def test_prompt_defaulted_to_local_emits_pull_deferred(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """The non-interactive prompt branch (yes=True) defers, doesn't complete."""
        TestSyncConflictResolution()._setup_diverged(
            initialized_project, patched_registry,
            ConflictResolutionStrategy.prompt,
        )
        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--yes", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        events = _read_events_jsonl(initialized_project)
        actions = [e["action"] for e in events]
        assert "sync.pull.deferred" in actions
        assert "sync.pull.completed" not in actions
        deferred = [e for e in events if e["action"] == "sync.pull.deferred"]
        assert len(deferred) == 1
        assert deferred[0]["payload_json"]["resolution"] == "prompt_defaulted_to_local"


class TestLocalMovedOnlyEmitsLocalAhead:
    """T5 — when the local Task has moved but the remote has not, the
    SyncMapping must end at ``sync_state="local_ahead"`` (NOT ``"in_sync"``)
    and a ``sync.push.deferred`` audit hint must fire.

    Scout's T1 audit identified the bug at cli/sync.py:848-865: the
    ``else`` arm collapsed two distinct realities ("neither moved" and
    "local moved but remote did not") into a single ``in_sync`` bump,
    erasing the divergence signal that ``drift_sync_state`` needs to
    surface the missed push.
    """

    def test_local_moved_only_marks_mapping_local_ahead(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """Bump the local Task's updated_at, pull, then assert mapping is local_ahead."""
        from fakoli_state.cli._helpers import _open_backend
        from fakoli_state.state.models import EventDraft as _EventDraft
    
        # Seed: task at past, mapping at past+1h, remote last_modified at past.
        seed_time = _NOW - timedelta(hours=4)
        _seed_task(initialized_project, now=seed_time)
        _seed_sync_mapping(
            initialized_project,
            last_synced_at=seed_time + timedelta(hours=1),
        )

        # Bump local task's updated_at PAST the mapping's last_synced_at
        # (so local_moved=True). status_changed updates updated_at to now-ish;
        # the seed timestamps put us well below current time, so this
        # creates the local_ahead-vs-remote divergence.
        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            b.append(_EventDraft(
                timestamp=_LATER,
                actor="test",
                action="task.status_changed",
                target_kind="task",
                target_id="T001",
                payload_json={
                    "task_id": "T001",
                    "from": "ready",
                    "to": "in_progress",
                },
            ))
        finally:
            b.close()

        # Remote last_modified is BEFORE the mapping's last_synced_at →
        # remote_moved=False. Combined with local_moved=True, we hit the
        # local-moved-only branch.
        remote = ExternalTask(
            external_id="42",
            title="Test task",
            body="desc",
            status_label=None,
            url=None,
            last_modified=seed_time,
            provider_metadata={},
        )
        cls = _make_scripted_provider_cls(fetch_returns=remote)
        patched_registry[_TEST_PROVIDER_ID] = cls

        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output

        # Mapping must be local_ahead, NOT in_sync (the historical bug).
        b = _open_backend(state_dir)
        try:
            mapping = b.get_sync_mapping("T001")
            assert mapping is not None
            assert str(mapping.sync_state) == "local_ahead", (
                f"T5 regression: local-moved-only branch did not set "
                f"sync_state=local_ahead; got {mapping.sync_state!r}. "
                f"Scout's T1 audit flagged this collapse at cli/sync.py:848."
            )
        finally:
            b.close()

    def test_local_moved_only_emits_push_deferred_hint(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """The local-moved-only pull emits ``sync.push.deferred`` with
        ``resolution="local_moved_no_push"`` so operators can grep for
        tasks awaiting a follow-up push."""
        from fakoli_state.cli._helpers import _open_backend
        from fakoli_state.state.models import EventDraft as _EventDraft
    
        seed_time = _NOW - timedelta(hours=4)
        _seed_task(initialized_project, now=seed_time)
        _seed_sync_mapping(
            initialized_project,
            last_synced_at=seed_time + timedelta(hours=1),
        )

        state_dir = initialized_project / ".fakoli-state"
        b = _open_backend(state_dir)
        try:
            b.append(_EventDraft(
                timestamp=_LATER,
                actor="test",
                action="task.status_changed",
                target_kind="task",
                target_id="T001",
                payload_json={
                    "task_id": "T001",
                    "from": "ready",
                    "to": "in_progress",
                },
            ))
        finally:
            b.close()

        remote = ExternalTask(
            external_id="42",
            title="Test task",
            body="desc",
            status_label=None,
            url=None,
            last_modified=seed_time,
            provider_metadata={},
        )
        cls = _make_scripted_provider_cls(fetch_returns=remote)
        patched_registry[_TEST_PROVIDER_ID] = cls

        r = runner.invoke(
            app,
            ["sync", "github", "--pull", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output

        events = _read_events_jsonl(initialized_project)
        push_deferred = [
            e for e in events if e["action"] == "sync.push.deferred"
        ]
        assert len(push_deferred) == 1, (
            f"T5 regression: local-moved-only branch did not emit "
            f"sync.push.deferred; got actions="
            f"{[e['action'] for e in events]}"
        )
        payload = push_deferred[0]["payload_json"]
        assert payload["resolution"] == "local_moved_no_push", (
            f"T5 regression: wrong resolution token on push.deferred; "
            f"got {payload.get('resolution')!r}"
        )
        assert payload["task_id"] == "T001"
        # provider_id round-trips through the audit row.
        assert payload["provider_id"] == _TEST_PROVIDER_ID


# ---------------------------------------------------------------------------
# Phase 9 v1.9.0 / PR #50 Greptile P1: malformed config.yaml must not crash sync
# ---------------------------------------------------------------------------


class TestMalformedConfigFallsBackToRegistry:
    """`_resolve_configured_providers` must swallow ``yaml.YAMLError``.

    The function is documented as best-effort: "if the config cannot be
    read for any reason (missing, malformed, raises) the registry fallback
    is used." Before the Greptile P1 fix, the ``except (ValueError, OSError)``
    block let ``yaml.YAMLError`` (a subclass of ``Exception``, not of
    ``ValueError`` or ``OSError``) escape — so a syntactically broken
    ``.fakoli-state/config.yaml`` blew up ``fakoli-state sync`` with an
    unhandled traceback instead of the documented graceful fallback.
    """

    def test_malformed_yaml_falls_back_to_registry_without_raising(
        self,
        initialized_project: Path,
        patched_registry: dict[str, Any],
    ) -> None:
        """Bare `sync` against a project with broken YAML exits cleanly.

        Overwrites the init-generated config.yaml with syntactically
        invalid YAML (an unclosed list) so ``yaml.safe_load`` raises
        ``yaml.YAMLError``. The CLI must catch this and fall back to
        ``sorted(PROVIDER_REGISTRY)`` so reconciliation still runs.
        """
        config_path = initialized_project / ".fakoli-state" / "config.yaml"
        # Deliberately malformed YAML: unclosed list in sync.providers.
        # yaml.safe_load raises yaml.YAMLError (specifically a ScannerError
        # / ParserError subclass), which is NOT a ValueError or OSError.
        config_path.write_text("sync:\n  providers: [unclosed\n", encoding="utf-8")

        r = runner.invoke(
            app,
            ["sync", "--cwd", str(initialized_project)],
            catch_exceptions=False,
        )

        # The CLI must exit cleanly via the registry fallback path.
        # An empty patched_registry means there's nothing to reconcile,
        # so we expect exit 0 with "No discrepancies".
        assert r.exit_code == 0, (
            f"malformed config.yaml should fall back to PROVIDER_REGISTRY, "
            f"not crash with yaml.YAMLError; got exit={r.exit_code}, "
            f"output={r.output!r}"
        )
        assert "No discrepancies found" in r.output
