"""sync sub-app: reconciliation + provider push/pull (Phase 8 Wave 3 Task 6).

User-facing surface for anvil's bidirectional sync stack.

Layout
------
``anvil sync``                bare command — runs ReconciliationEngine
``anvil sync --fix --yes``    same, then applies suggested fixes
``anvil sync github``         alias for ``sync provider github_issues``
``anvil sync provider <id>``  generic provider invocation
                                     (``--push`` / ``--pull`` / ``--task`` / ``--prd``)
``anvil sync github --health``probe provider reachability + auth
``anvil sync github --watch`` long-running poll loop

Design notes
------------
* The bare ``sync`` callback is the default (``invoke_without_command=True``);
  named subcommands (``github``, ``provider``) win when explicitly invoked.
* The ``github`` alias is registered as its own ``@sync_app.command`` so it
  shows up in ``--help`` and so Typer's resolver picks it over the generic
  ``provider`` subcommand without a precedence dance.
* Every sync mutation emits a ``sync.*`` audit event into events.jsonl /
  state.db via the SyncAuditPayload no-op handler added in Wave 3 to
  state/sqlite.py.  Actual SyncMapping persistence flows through
  ``backend.apply_sync_mapping(...)`` which emits ``sync_mapping.upserted``.
* No real network calls happen in tests — every test injects a
  RecordedSyncProvider (or a custom subclass) via monkeypatching
  ``PROVIDER_REGISTRY`` so the ``provider_id`` resolves to the test double.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
import yaml

from anvil.cli._helpers import (
    PRD_OPTION,
    _open_backend,
    _require_state_dir,
    _resolve_project_root,
    _resolve_state_dir,
    canonical_prd_id,
)

if TYPE_CHECKING:
    from anvil.state.models import Task
    from anvil.state.sqlite import SqliteBackend
    from anvil.sync.provider import (
        ExternalRef,
        ExternalTask,
        ProviderHealth,
        SyncProvider,
    )

__all__ = ["sync_app"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The github alias maps to this canonical provider id.
_GITHUB_PROVIDER_ID = "github_issues"

# Subdirectory under <state_dir> where manual_merge conflict files are written.
_SYNC_CONFLICTS_DIRNAME = ".sync-conflicts"

# Exit codes — match other anvil CLI commands.
_EXIT_OK = 0
_EXIT_GENERIC_ERROR = 1
_EXIT_NEEDS_OPERATOR_INPUT = 2


# ---------------------------------------------------------------------------
# App definition
# ---------------------------------------------------------------------------

sync_app = typer.Typer(
    name="sync",
    help=(
        "Bidirectional sync surface. Bare `sync` runs reconciliation; "
        "`sync github` push/pulls via the GitHub Issues provider; "
        "`sync provider <id>` invokes a contributor-registered provider."
    ),
    no_args_is_help=False,
)


# ---------------------------------------------------------------------------
# Bare `sync` — reconciliation entrypoint (default callback)
# ---------------------------------------------------------------------------


@sync_app.callback(invoke_without_command=True)
def sync_default(
    ctx: typer.Context,
    fix: bool = typer.Option(  # noqa: B008
        False,
        "--fix",
        help=(
            "After scanning, apply each suggested fix. Requires --yes "
            "in non-interactive mode."
        ),
    ),
    yes: bool = typer.Option(  # noqa: B008
        False,
        "--yes",
        help="Skip the confirmation prompt before applying fixes.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Run reconciliation and (optionally) apply remediation.

    No subcommand → scan ReconciliationEngine and print the report. The
    `--fix` flag additionally applies each suggested fix; combine with
    `--yes` in CI / non-interactive contexts.

    Named subcommands (`github`, `provider`) take over when invoked — this
    body only runs when the user types `anvil sync` with no
    subcommand.
    """
    if ctx.invoked_subcommand is not None:
        # A subcommand was requested; let it handle the run.
        return

    state_dir = _resolve_state_dir(cwd)
    project_root = _resolve_project_root(cwd)
    _require_state_dir(state_dir)
    backend = _open_backend(state_dir)
    try:
        report = _run_reconciliation(backend, state_dir, project_root)
        _print_reconciliation_report(report)

        if not fix:
            return

        # --fix path — apply remediations.
        if not yes and not _is_tty():
            typer.echo(
                "Error: --fix requires --yes in non-interactive mode "
                "(refusing to apply remediations without explicit consent).",
                err=True,
            )
            raise typer.Exit(code=_EXIT_GENERIC_ERROR)
        if not yes:
            confirmed = typer.confirm(
                f"Apply {len(report.discrepancies)} fix(es)?",
                default=False,
            )
            if not confirmed:
                typer.echo("Aborted.")
                raise typer.Exit(code=_EXIT_OK)

        actions = _apply_reconciliation_fixes(
            backend, state_dir, report, project_root
        )
        _print_fix_actions(actions)
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# `sync github` alias — maps to provider_id="github_issues"
# ---------------------------------------------------------------------------


@sync_app.command("github")
def sync_github(
    push: bool = typer.Option(  # noqa: B008
        False, "--push", help="Push local tasks to GitHub only (skip pull)."
    ),
    pull: bool = typer.Option(  # noqa: B008
        False, "--pull", help="Pull remote issues to local only (skip push)."
    ),
    watch: bool = typer.Option(  # noqa: B008
        False, "--watch", help="Long-running poll loop; Ctrl-C to exit."
    ),
    fix: bool = typer.Option(  # noqa: B008
        False,
        "--fix",
        help=(
            "Reconcile remote state into local on conflicts (forces a pull "
            "for tasks whose SyncMapping is in 'conflict' state)."
        ),
    ),
    task: str | None = typer.Option(  # noqa: B008
        None, "--task", help="Scope sync to a single task id (e.g. T001)."
    ),
    prd: str | None = PRD_OPTION,
    yes: bool = typer.Option(  # noqa: B008
        False,
        "--yes",
        help="Auto-confirm conflict prompts; defaults to local_wins.",
    ),
    health: bool = typer.Option(  # noqa: B008
        False,
        "--health",
        help="Probe provider reachability + auth; print status; exit.",
    ),
    interval: int = typer.Option(  # noqa: B008
        60,
        "--interval",
        help="Poll interval seconds (with --watch). Use 0 for one iteration.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Sync against GitHub Issues. Convenience alias for `sync provider github_issues`."""
    _sync_provider_dispatch(
        provider_id=_GITHUB_PROVIDER_ID,
        push=push,
        pull=pull,
        watch=watch,
        fix=fix,
        task=task,
        yes=yes,
        health=health,
        interval=interval,
        cwd=cwd,
        prd=prd,
    )


# ---------------------------------------------------------------------------
# `sync provider <id>` — generic provider invocation
# ---------------------------------------------------------------------------


@sync_app.command("provider")
def sync_provider(
    provider_id: str = typer.Argument(  # noqa: B008
        ..., help="Sync provider id (e.g. github_issues, monday, linear)."
    ),
    push: bool = typer.Option(  # noqa: B008
        False, "--push", help="Push local tasks only (skip pull)."
    ),
    pull: bool = typer.Option(  # noqa: B008
        False, "--pull", help="Pull remote tasks only (skip push)."
    ),
    watch: bool = typer.Option(  # noqa: B008
        False, "--watch", help="Long-running poll loop; Ctrl-C to exit."
    ),
    fix: bool = typer.Option(  # noqa: B008
        False,
        "--fix",
        help="Reconcile remote → local on conflicts (forces a pull on conflict).",
    ),
    task: str | None = typer.Option(  # noqa: B008
        None, "--task", help="Scope sync to a single task id."
    ),
    prd: str | None = PRD_OPTION,
    yes: bool = typer.Option(  # noqa: B008
        False, "--yes", help="Auto-confirm conflict prompts."
    ),
    health: bool = typer.Option(  # noqa: B008
        False, "--health", help="Probe provider; print status; exit."
    ),
    interval: int = typer.Option(  # noqa: B008
        60, "--interval", help="Poll interval seconds (with --watch)."
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Push/pull against a registered sync provider by id."""
    _sync_provider_dispatch(
        provider_id=provider_id,
        push=push,
        pull=pull,
        watch=watch,
        fix=fix,
        task=task,
        yes=yes,
        health=health,
        interval=interval,
        cwd=cwd,
        prd=prd,
    )


# ---------------------------------------------------------------------------
# Provider dispatch — shared between `github` alias and `provider` generic
# ---------------------------------------------------------------------------


def _sync_provider_dispatch(
    *,
    provider_id: str,
    push: bool,
    pull: bool,
    watch: bool,
    fix: bool,
    task: str | None,
    yes: bool,
    health: bool,
    interval: int,
    cwd: Path | None,
    prd: str | None = None,
) -> None:
    """Single entrypoint for both ``sync github`` and ``sync provider <id>``.

    Branches on ``health`` (probe only) vs the full sync flow. The full flow
    is wrapped in a loop when ``--watch`` is set, otherwise runs once.
    """
    state_dir = _resolve_state_dir(cwd)

    # Resolve provider class via the registry. The provider lookup is a
    # pure dict lookup — no state-dir required.
    provider_cls = _resolve_provider(provider_id)

    # Instantiate. Providers may require env vars (GitHubIssuesProvider needs
    # GITHUB_REPOSITORY or repo kwarg) — surface their ValueError as a
    # friendly CLI error rather than a stack trace.
    try:
        provider = provider_cls()
    except (ValueError, TypeError) as exc:
        typer.echo(f"Error: cannot instantiate provider {provider_id!r}: {exc}", err=True)
        raise typer.Exit(code=_EXIT_GENERIC_ERROR) from exc

    if health:
        # Health is a network/auth probe — no local state is required.
        # Surface it before _require_state_dir so operators can sanity-check
        # GITHUB_TOKEN / connectivity from a fresh checkout pre-init.
        _print_provider_health(provider)
        return

    # Now (and only now) the state dir is required for actual sync ops.
    _require_state_dir(state_dir)
    backend = _open_backend(state_dir)
    try:
        if watch:
            _run_watch_loop(
                backend=backend,
                state_dir=state_dir,
                provider=provider,
                push=push,
                pull=pull,
                fix=fix,
                task=task,
                yes=yes,
                interval=interval,
                prd=prd,
            )
        else:
            _run_sync_once(
                backend=backend,
                state_dir=state_dir,
                provider=provider,
                push=push,
                pull=pull,
                fix=fix,
                task=task,
                yes=yes,
                prd=prd,
            )
    finally:
        # Providers may hold an ``httpx.Client`` (or other transport pool);
        # in --watch mode the dispatch lives for hours, so explicit
        # cleanup avoids the unclosed-transport warning that fires on
        # interpreter shutdown. Providers without close() (contributor
        # test doubles) get a duck-typed pass.
        close_fn = getattr(provider, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:  # noqa: BLE001 — close() must not abort shutdown
                pass
        backend.close()


# ---------------------------------------------------------------------------
# Registry resolution + friendly error
# ---------------------------------------------------------------------------


def _resolve_provider(provider_id: str) -> type[SyncProvider]:
    """Return the registered provider class, or exit 1 with a helpful list.

    Delegates to :func:`anvil.sync.registry.get_sync_provider` so the
    "available providers: ..." hint is produced in exactly one place; on
    miss we re-surface the KeyError message via typer.echo + exit 1.

    Late import keeps cli.sync importable in environments where the sync
    subpackage has not been touched (e.g. a ``--help`` smoke test before
    init). The function is called on every invocation so test
    monkeypatching of ``PROVIDER_REGISTRY`` survives — Typer command
    bodies execute many times per test session.
    """
    from anvil.sync.registry import get_sync_provider

    try:
        return get_sync_provider(provider_id)
    except KeyError as exc:
        # KeyError's str() is the message wrapped in repr quotes; strip them.
        message = exc.args[0] if exc.args else str(exc)
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(code=_EXIT_GENERIC_ERROR) from exc


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


def _print_provider_health(provider: SyncProvider) -> None:
    """Print a one-screen summary of provider.health_check()."""
    health: ProviderHealth = provider.health_check()
    typer.echo(f"Provider: {provider.display_name} ({provider.provider_id})")
    typer.echo(f"  available:        {health.available}")
    typer.echo(f"  auth_configured:  {health.auth_configured}")
    typer.echo(f"  last_check_at:    {health.last_check_at.isoformat()}")
    if health.error:
        typer.echo(f"  error:            {health.error}")


# ---------------------------------------------------------------------------
# One-shot sync pass
# ---------------------------------------------------------------------------


def _run_sync_once(
    *,
    backend: SqliteBackend,
    state_dir: Path,
    provider: SyncProvider,
    push: bool,
    pull: bool,
    fix: bool,
    task: str | None,
    yes: bool,
    prd: str | None = None,
) -> None:
    """Execute one push+pull cycle through ``provider``.

    Defaults (neither ``push`` nor ``pull``) run BOTH. Mutually exclusive
    only in that ``--push`` skips pull and vice versa.

    ``--task T001`` scopes to a single task; otherwise every task gets a
    sync attempt. ``--prd <id>`` (T027) scopes the batch to one PRD's tasks
    (default: every PRD). ``--fix`` swaps the conflict path to a forced pull
    (remote_wins on every conflict).
    """
    # Default: do both. If only --push or --pull is set, do that side only.
    do_push = push or not pull
    do_pull = pull or not push

    _scope_note = f"task={task}" if task else (f"prd={prd}" if prd else None)
    _emit_audit(
        backend,
        action="sync.batch.started",
        payload={
            "provider_id": provider.provider_id,
            "direction": "push" if (do_push and not do_pull) else (
                "pull" if (do_pull and not do_push) else "both"
            ),
            "audit_note": _scope_note,
        },
        target_kind="provider",
        target_id=provider.provider_id,
    )

    tasks = _select_tasks_for_sync(backend, task, prd)
    if not tasks:
        typer.echo("Nothing to sync (no matching tasks).")
        _emit_audit(
            backend,
            action="sync.batch.completed",
            payload={
                "provider_id": provider.provider_id,
                "audit_note": "no tasks",
            },
            target_kind="provider",
            target_id=provider.provider_id,
        )
        return

    push_results = {"pushed": 0, "failed": 0, "skipped": 0}
    pull_results: dict[str, int] = {
        "pulled": 0,
        "failed": 0,
        "skipped": 0,
        "manual_merge_pending": 0,
    }
    for t in tasks:
        if do_push:
            _push_one_task(
                backend=backend,
                provider=provider,
                task=t,
                results=push_results,
            )
        if do_pull:
            _pull_one_task(
                backend=backend,
                state_dir=state_dir,
                provider=provider,
                task=t,
                results=pull_results,
                fix=fix,
                yes=yes,
            )

    typer.echo(
        f"Sync against {provider.display_name} ({provider.provider_id}): "
        f"push={push_results} pull={pull_results}"
    )

    _emit_audit(
        backend,
        action="sync.batch.completed",
        payload={
            "provider_id": provider.provider_id,
            "audit_note": (
                f"pushed={push_results['pushed']} pulled={pull_results['pulled']} "
                f"failed_push={push_results['failed']} failed_pull={pull_results['failed']} "
                f"manual_merge_pending={pull_results['manual_merge_pending']}"
            ),
        },
        target_kind="provider",
        target_id=provider.provider_id,
    )

    # Exit non-zero if any task is parked awaiting manual-merge resolution.
    # We process the whole batch first (so unrelated tasks still sync),
    # then surface the operator-input requirement via exit code 2.
    if pull_results["manual_merge_pending"] > 0:
        raise typer.Exit(code=_EXIT_NEEDS_OPERATOR_INPUT)


def _select_tasks_for_sync(
    backend: SqliteBackend,
    task_filter: str | None,
    prd_filter: str | None = None,
) -> list[Task]:
    """Return the list of tasks to sync this iteration.

    ``task_filter`` (e.g. ``"T001"``) narrows to one task; missing → empty
    list with a friendly print upstream.

    ``prd_filter`` (T027) scopes the batch to the tasks OWNED by one PRD —
    ``anvil sync provider <id> --prd <prd_id>`` pushes only that PRD's tasks.
    Its value comes from the shared ``--prd`` / ``$ANVIL_PRD`` wiring
    (``PRD_OPTION``), so the env override is honoured identically to every
    other PRD-scoped command. ``None`` (the default, no ``--prd`` and no
    ``$ANVIL_PRD``) iterates every PRD's tasks. The default-PRD sentinel
    ``'prd'`` is collapsed to the stored id ``'default'`` via
    :func:`canonical_prd_id` so ``--prd prd`` matches the default partition's
    rows (which store ``prd_id='default'``) instead of silently matching none.
    A ``--task`` id wins over ``--prd``: an explicit single-task scope is the
    narrowest selection, so the PRD filter is ignored when ``task_filter`` is
    set.
    """
    if task_filter is not None:
        one = backend.get_task(task_filter)
        return [one] if one is not None else []
    resolved_prd = canonical_prd_id(prd_filter) if prd_filter is not None else None
    return backend.list_tasks(prd_id=resolved_prd)


# ---------------------------------------------------------------------------
# Push one task
# ---------------------------------------------------------------------------


def _push_one_task(
    *,
    backend: SqliteBackend,
    provider: SyncProvider,
    task: Task,
    results: dict[str, int],
) -> None:
    """Push a single task via ``provider``. Updates ``results`` in place.

    Reads the existing SyncMapping (if any) and uses its external_id as
    the input ``mapping`` ExternalRef to push_task. On success, upserts
    the SyncMapping with the freshly-returned ExternalRef.
    """
    from anvil.sync.provider import ExternalRef

    existing = backend.get_sync_mapping(task.id, external_system=provider.provider_id)
    in_ref: ExternalRef | None = None
    if existing is not None:
        in_ref = ExternalRef(
            provider_id=provider.provider_id,
            external_id=existing.external_id,
            url=existing.external_url,
        )

    _emit_audit(
        backend,
        action="sync.push.started",
        payload={
            "provider_id": provider.provider_id,
            "task_id": task.id,
            "external_id": existing.external_id if existing else None,
            "direction": "push",
        },
        target_kind="task",
        target_id=task.id,
    )

    try:
        out_ref = provider.push_task(task=task, mapping=in_ref)
    except Exception as exc:  # noqa: BLE001 — best-effort wrapping loop
        results["failed"] += 1
        _emit_audit(
            backend,
            action="sync.push.failed",
            payload={
                "provider_id": provider.provider_id,
                "task_id": task.id,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
                "direction": "push",
            },
            target_kind="task",
            target_id=task.id,
        )
        typer.echo(
            f"  push T={task.id} failed ({type(exc).__name__}): {exc}",
            err=True,
        )
        return

    # Persist the mapping via the canonical sync_mapping.upserted event.
    _persist_mapping_from_push(
        backend=backend,
        task=task,
        provider=provider,
        external_ref=out_ref,
        existing=existing,
    )

    results["pushed"] += 1
    _emit_audit(
        backend,
        action="sync.push.completed",
        payload={
            "provider_id": provider.provider_id,
            "task_id": task.id,
            "external_id": out_ref.external_id,
            "direction": "push",
        },
        target_kind="task",
        target_id=task.id,
    )


def _persist_mapping_from_push(
    *,
    backend: SqliteBackend,
    task: Task,
    provider: SyncProvider,
    external_ref: ExternalRef,
    existing: Any,
) -> None:
    """Write or refresh the SyncMapping row after a successful push.

    The provider id is persisted as a plain string. No ``ExternalSystem``
    enum gate: contributor providers (e.g. ``"monday"``, ``"linear"``,
    ``"my_custom_tracker"``) MUST be able to round-trip through
    ``apply_sync_mapping`` without first patching the canonical enum.
    The DB column is TEXT and the prior gate caused every contributor
    sync cycle to create a duplicate remote record (no mapping written →
    next push has ``existing=None`` → creates a new issue/card/etc.).
    """
    from anvil.clock import SystemClock
    from anvil.state.models import (
        ConflictResolutionStrategy,
        SyncMapping,
        SyncState,
    )

    strategy = (
        existing.conflict_resolution_strategy
        if existing is not None
        else ConflictResolutionStrategy.prompt
    )
    # Stamp the task's OWNING prd_id (T027): a task-kind mapping is owned by the
    # PRD that owns the task, so the persisted SyncMapping carries task.prd_id.
    # A task whose prd_id was never set falls back to the default partition via
    # apply_sync_mapping's None→DEFAULT_PRD_ID coercion. entity_kind stays
    # 'task' (the model default) for every CLI push — milestone/prd-kind
    # mappings are a deferred capability (T029).
    mapping = SyncMapping(
        task_id=task.id,
        external_system=provider.provider_id,
        external_id=external_ref.external_id,
        external_url=external_ref.url,
        last_synced_at=SystemClock().now(),
        sync_state=SyncState.in_sync,
        conflict_resolution_strategy=strategy,
        provider_metadata=dict(existing.provider_metadata) if existing else {},
        prd_id=task.prd_id,
    )
    backend.apply_sync_mapping(mapping, actor="sync-cli")


# ---------------------------------------------------------------------------
# Pull one task
# ---------------------------------------------------------------------------


def _pull_one_task(
    *,
    backend: SqliteBackend,
    state_dir: Path,
    provider: SyncProvider,
    task: Task,
    results: dict[str, int],
    fix: bool,
    yes: bool,
) -> None:
    """Pull the remote payload for ``task`` via ``provider``.

    Only attempts a pull when a SyncMapping exists (no remote id to fetch
    by otherwise). On divergence (remote `last_modified` > local `updated_at`
    AND local has changed), branches on the SyncMapping's
    conflict_resolution_strategy.
    """
    from anvil.state.models import ConflictResolutionStrategy

    existing = backend.get_sync_mapping(task.id, external_system=provider.provider_id)
    if existing is None:
        results["skipped"] += 1
        return

    _emit_audit(
        backend,
        action="sync.pull.started",
        payload={
            "provider_id": provider.provider_id,
            "task_id": task.id,
            "external_id": existing.external_id,
            "direction": "pull",
        },
        target_kind="task",
        target_id=task.id,
    )

    try:
        remote: ExternalTask | None = provider.fetch_task(external_id=existing.external_id)
    except Exception as exc:  # noqa: BLE001 — best-effort loop
        results["failed"] += 1
        _emit_audit(
            backend,
            action="sync.pull.failed",
            payload={
                "provider_id": provider.provider_id,
                "task_id": task.id,
                "external_id": existing.external_id,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
                "direction": "pull",
            },
            target_kind="task",
            target_id=task.id,
        )
        typer.echo(
            f"  pull T={task.id} failed ({type(exc).__name__}): {exc}",
            err=True,
        )
        return

    if remote is None:
        # Tombstone — remote deleted. Surface in stderr but don't fail.
        typer.echo(
            f"  pull T={task.id}: remote {existing.external_id!r} is gone "
            "(external_deleted)",
            err=True,
        )
        # SF-5: flip the mapping's sync_state to ``external_deleted`` so
        # the reconciliation engine's drift scan can surface tombstoned
        # mappings. Before this fix the mapping stayed at whatever it
        # was (typically ``in_sync``) and the operator had no way to
        # discover the dangling reference except by manually running
        # pull and reading stderr.
        from anvil.clock import SystemClock
        from anvil.state.models import SyncState

        _bump_mapping_state(
            backend=backend,
            existing=existing,
            new_state=SyncState.external_deleted,
            clock_now=SystemClock().now(),
            actor=f"sync.{provider.provider_id}",
        )
        results["pulled"] += 1
        _emit_audit(
            backend,
            action="sync.pull.completed",
            payload={
                "provider_id": provider.provider_id,
                "task_id": task.id,
                "external_id": existing.external_id,
                "audit_note": "external_deleted",
                "direction": "pull",
            },
            target_kind="task",
            target_id=task.id,
        )
        return

    # Conflict detection: remote moved after our last sync AND local moved
    # too. If only one side moved we still record the pull but no conflict.
    remote_moved = remote.last_modified > existing.last_synced_at
    local_moved = task.updated_at > existing.last_synced_at

    # Track whether this iteration produced a real local mutation. Used
    # downstream to pick the honest terminal event:
    #   * sync.pull.completed → ``mutated_local`` is True (clean pull bumps
    #     the mapping; tombstone flips to external_deleted; remote-moved-only
    #     rewrites the Task; immediate-apply conflict branches do likewise).
    #   * sync.pull.deferred  → the branch recorded an intent only
    #     (manual_merge file written; 6 deferred conflict-resolution
    #     branches that have not yet been wired to mutate inline).
    # Also tracks the resolution token for the deferred branches so the
    # audit row is self-describing (e.g. "local_wins_deferred",
    # "prompt_chose_remote", "manual_merge_pending"). The resolution
    # token is the value returned by ``_resolve_conflict`` and is passed
    # straight into the audit payload below (Wave 3 critic CONSIDER #1 /
    # Greptile P2, PR #50: no need for an intermediate variable).

    if remote_moved and local_moved:
        # --fix forces remote_wins for this run.
        strategy = (
            ConflictResolutionStrategy.remote_wins
            if fix
            else existing.conflict_resolution_strategy
        )
        resolved, applied, resolution = _resolve_conflict(
            backend=backend,
            state_dir=state_dir,
            provider=provider,
            task=task,
            remote=remote,
            strategy=strategy,
            yes=yes,
            existing=existing,
        )
        if not resolved:
            # manual_merge: file was written, task is parked pending
            # operator action. We do NOT count this as a hard failure
            # (the network call succeeded); we count it as pending so
            # the batch can exit 2 at the end while still processing
            # subsequent tasks.
            results["manual_merge_pending"] = (
                results.get("manual_merge_pending", 0) + 1
            )
            # PR#49 MF: emit a terminal `sync.pull.deferred` audit
            # event. ``sync.pull.started`` already fired above, so
            # without this the audit log contains a dangling start
            # with no terminal — operators monitoring events.jsonl
            # could not disambiguate a parked manual_merge from a
            # process crash mid-pull.
            _emit_audit(
                backend,
                action="sync.pull.deferred",
                payload={
                    "provider_id": provider.provider_id,
                    "task_id": task.id,
                    "external_id": existing.external_id,
                    "direction": "pull",
                    "resolution": resolution,
                    "audit_note": "manual_merge_pending",
                },
                target_kind="task",
                target_id=task.id,
            )
            return
        if not applied:
            # T5 audit-honesty repointing: the resolution recorded an
            # intent (local_wins / remote_wins / prompt_*) but no local
            # Task mutation happened in this iteration. The historical
            # `sync.pull.completed` terminal lied — see scout's T1
            # status file (six "DISHONEST" branches at cli/sync.py:1044,
            # 1059, 1075, 1088, 1091, 1094). Switch to the honest
            # `sync.pull.deferred` terminal so operators monitoring the
            # audit stream can tell deferred-intent apart from a true
            # local-state mutation. The mapping bookkeeping
            # (`_bump_mapping_state` inside `_resolve_conflict`) still
            # ran, so subsequent polls do not re-detect the same
            # conflict — only the terminal name changes.
            results["pulled"] += 1
            _emit_audit(
                backend,
                action="sync.pull.deferred",
                payload={
                    "provider_id": provider.provider_id,
                    "task_id": task.id,
                    "external_id": existing.external_id,
                    "direction": "pull",
                    "resolution": resolution,
                },
                target_kind="task",
                target_id=task.id,
            )
            return
    elif remote_moved and not local_moved:
        # Pull-applies-remote (P1-1): remote moved ahead, local is untouched
        # since last_synced_at — apply the remote payload to the local task
        # and refresh the SyncMapping to in_sync. Without this branch the
        # audit log lies ("pull completed" but local state unchanged).
        _apply_remote_to_local(
            backend=backend,
            provider=provider,
            task=task,
            remote=remote,
            existing=existing,
        )
    elif local_moved and not remote_moved:
        # T5 local_moved-only path: the local Task carries unsynced edits
        # but the remote has not moved. Pre-fix (scout T1 audit, line
        # 848-865) this branch collapsed into the `else` arm and bumped
        # the mapping to ``SyncState.in_sync`` — actively erasing the
        # divergence signal that reconciliation's `drift_sync_state` scan
        # needs to surface the missed push. Correct semantics: mark the
        # mapping ``local_ahead`` (we know the local copy is ahead), and
        # emit ``sync.push.deferred`` with ``resolution="local_moved_no_push"``
        # so operators can grep for tasks awaiting a follow-up push.
        # Reuses the existing ``sync.push.deferred`` action per guido T3's
        # escalation — do NOT widen the discriminated union with a new
        # action string.
        from anvil.clock import SystemClock
        from anvil.state.models import SyncState

        _bump_mapping_state(
            backend=backend,
            existing=existing,
            new_state=SyncState.local_ahead,
            clock_now=SystemClock().now(),
            actor=f"sync.{provider.provider_id}",
        )
        _emit_audit(
            backend,
            action="sync.push.deferred",
            payload={
                "provider_id": provider.provider_id,
                "task_id": task.id,
                "external_id": existing.external_id,
                "direction": "push",
                "resolution": "local_moved_no_push",
                "audit_note": "local task ahead of remote; run --push to advance",
            },
            target_kind="task",
            target_id=task.id,
        )
        # The pull itself is honest — we did fetch the remote, we did
        # observe no remote movement, and we bumped the mapping to a
        # truthful state. Emit the pull terminal too so the start/end
        # pair is closed; the separate push.deferred event records the
        # push hint.
    else:
        # SF-15: clean pull — neither side moved. The pull succeeded —
        # bump last_synced_at so the 7-day drift_sync_state scan does
        # not flag this mapping as "stale" just because no remote write
        # happened. Without this, any task that's been pull-only-synced
        # for a week surfaces as drift even if pulls have been happening
        # every minute.
        from anvil.clock import SystemClock
        from anvil.state.models import SyncState

        _bump_mapping_state(
            backend=backend,
            existing=existing,
            new_state=SyncState.in_sync,
            clock_now=SystemClock().now(),
            actor=f"sync.{provider.provider_id}",
        )

    results["pulled"] += 1
    _emit_audit(
        backend,
        action="sync.pull.completed",
        payload={
            "provider_id": provider.provider_id,
            "task_id": task.id,
            "external_id": existing.external_id,
            "direction": "pull",
        },
        target_kind="task",
        target_id=task.id,
    )


def _bump_mapping_state(
    *,
    backend: SqliteBackend,
    existing: Any,
    new_state: Any,
    clock_now: Any,
    actor: str,
) -> None:
    """Re-emit ``existing`` SyncMapping with a new ``sync_state`` and
    refreshed ``last_synced_at``.

    Called from each deferred-conflict-resolution branch (SF-4), the
    clean-pull path (SF-15), and the tombstone path (SF-5) so the next
    poll's ``remote_moved`` / ``local_moved`` comparison is against the
    resolution point — not the stale pre-conflict timestamp. Without
    this, every 60s ``--watch`` poll re-fires the same
    ``sync.conflict_detected`` event (1440 redundant events / task / day
    at the documented interval).

    ``new_state`` is a :class:`SyncState`. ``clock_now`` is a UTC
    datetime — typically ``SystemClock().now()``.
    """
    from anvil.state.models import SyncMapping

    refreshed = SyncMapping(
        task_id=existing.task_id,
        external_system=existing.external_system,
        external_id=existing.external_id,
        external_url=existing.external_url,
        last_synced_at=clock_now,
        sync_state=new_state,
        conflict_resolution_strategy=existing.conflict_resolution_strategy,
        provider_metadata=dict(existing.provider_metadata or {}),
    )
    backend.apply_sync_mapping(refreshed, actor=actor)


def _apply_remote_to_local(
    *,
    backend: SqliteBackend,
    provider: SyncProvider,
    task: Task,
    remote: ExternalTask,
    existing: Any,
) -> None:
    """Apply a remote ExternalTask payload to the local Task (P1-1).

    Called from the ``remote_moved and not local_moved`` branch of
    :func:`_pull_one_task`. Emits two events in sequence:

    1. ``task.synced_from_remote`` — rewrites the Task's title /
       description / status from the remote payload. Status is taken
       from the remote ``status_label`` only when it parses cleanly as
       a ``TaskStatus`` value (provider-native labels like
       ``"open"`` / ``"closed"`` do NOT, and we preserve local status
       in that case rather than crashing the pull).
    2. ``sync_mapping.upserted`` — bumps the mapping's
       ``last_synced_at`` to now and clears the ``sync_state`` back to
       ``in_sync`` (so subsequent reconciliation passes don't keep
       flagging the same drift).
    """
    from anvil.clock import SystemClock
    from anvil.state.models import (
        EventDraft,
        SyncMapping,
        SyncState,
        TaskStatus,
    )

    # Resolve a status from the remote payload when possible. Provider-
    # native labels (``"open"`` for GitHub) do not match a TaskStatus, so
    # we keep the local status in that case — the remote's status_label
    # vocabulary is a provider concern, not an anvil concern.
    new_status: str = str(task.status)
    if remote.status_label is not None:
        try:
            new_status = str(TaskStatus(remote.status_label))
        except ValueError:
            # Not a TaskStatus value — keep local. The provider may have
            # a richer mapping (e.g. label→status), but that translation
            # happens inside the provider's push_task / fetch_task; the
            # CLI sync surface only ever sees the literal status_label.
            new_status = str(task.status)

    clock = SystemClock()
    actor = f"sync.{provider.provider_id}"

    # 1. Rewrite the local Task.
    sync_draft = EventDraft(
        timestamp=clock.now(),
        actor=actor,
        action="task.synced_from_remote",
        target_kind="task",
        target_id=task.id,
        payload_json={
            "task_id": task.id,
            "title": remote.title,
            "description": remote.body,
            "status": new_status,
            "actor": actor,
        },
    )
    backend.append(sync_draft)

    # 2. Refresh the SyncMapping — clear conflict state, bump
    # last_synced_at to now.
    refreshed = SyncMapping(
        task_id=existing.task_id,
        external_system=existing.external_system,
        external_id=existing.external_id,
        external_url=existing.external_url,
        last_synced_at=clock.now(),
        sync_state=SyncState.in_sync,
        conflict_resolution_strategy=existing.conflict_resolution_strategy,
        provider_metadata=dict(existing.provider_metadata or {}),
    )
    backend.apply_sync_mapping(refreshed, actor=actor)


# ---------------------------------------------------------------------------
# Conflict resolution strategies
# ---------------------------------------------------------------------------


def _resolve_conflict(
    *,
    backend: SqliteBackend,
    state_dir: Path,
    provider: SyncProvider,
    task: Task,
    remote: ExternalTask,
    strategy: Any,
    yes: bool,
    existing: Any,
) -> tuple[bool, bool, str]:
    """Apply the configured conflict-resolution strategy.

    Returns a 3-tuple ``(resolved, applied, resolution)``:
    * ``resolved`` — True if the conflict was handled (any branch except
      ``manual_merge`` which returns False because operator action is
      required before the pull can continue).
    * ``applied`` — True if this iteration actually mutated the local
      Task (immediate-apply variants of ``local_wins`` / ``remote_wins``).
      False for the six deferred branches (``local_wins_deferred``,
      ``remote_wins_deferred``, ``prompt_defaulted_to_local``,
      ``prompt_chose_local``, ``prompt_chose_remote``, ``prompt_skipped``)
      where only the mapping bookkeeping advanced; the caller uses this
      to pick ``sync.pull.completed`` vs the honest ``sync.pull.deferred``.
    * ``resolution`` — the short token (e.g. ``"local_wins_deferred"``,
      ``"manual_merge_file_written"``) carried into the conflict /
      pull-deferred audit row so the JSONL is self-describing.

    Emits ``sync.conflict_detected`` regardless of resolution.

    SF-4: every branch ALSO bumps the SyncMapping's ``sync_state`` and
    ``last_synced_at`` via :func:`_bump_mapping_state` so the next poll's
    ``remote_moved``/``local_moved`` comparison is against the
    resolution point. Without this the same conflict re-fires every
    poll forever (1440 redundant events / task / day at the default
    60s interval).
    """
    from anvil.clock import SystemClock
    from anvil.state.models import ConflictResolutionStrategy, SyncState

    strategy_str = str(strategy)
    resolution: str
    # State to write to the mapping AFTER resolving. Defaults per
    # branch and captured in ``new_state`` so the bump is uniform.
    new_state: SyncState

    if strategy == ConflictResolutionStrategy.local_wins:
        # Record the choice; the actual local→remote re-push is deferred
        # to a subsequent push pass. We intentionally do NOT claim
        # "_applied" here because no mutation happens in this iteration —
        # the audit log would lie. See TODO below.
        typer.echo(
            f"  conflict T={task.id}: local_wins — local state will overwrite "
            "remote on the next push pass (deferred to subsequent iteration)",
            err=True,
        )
        # TODO(phase-9): wire immediate re-push so resolution can be
        # "local_wins_applied". Today the deferral relies on a
        # subsequent --push iteration or the same iteration's push pass.
        resolution = "local_wins_deferred"
        new_state = SyncState.local_ahead
    elif strategy == ConflictResolutionStrategy.remote_wins:
        # Same story for remote_wins — we record the decision but the
        # actual local mutation from the remote payload is deferred. No
        # task-mutation events exist outside the planning path today.
        typer.echo(
            f"  conflict T={task.id}: remote_wins — local will be overwritten "
            "by remote on the next pull pass (deferred to subsequent iteration)",
            err=True,
        )
        # TODO(phase-9): wire immediate local mutation event so resolution
        # can become "remote_wins_applied".
        resolution = "remote_wins_deferred"
        new_state = SyncState.remote_ahead
    elif strategy == ConflictResolutionStrategy.prompt:
        # Interactive prompt unless --yes / non-tty: defaults to local_wins
        # with a stderr warning per spec.
        if yes or not _is_tty():
            typer.echo(
                f"  conflict T={task.id}: prompt strategy non-interactive — "
                "defaulting to local_wins",
                err=True,
            )
            resolution = "prompt_defaulted_to_local"
            new_state = SyncState.local_ahead
        else:
            choice = typer.prompt(
                f"  conflict T={task.id}: choose [local/remote/skip]",
                default="skip",
            ).strip().lower()
            if choice.startswith("l"):
                resolution = "prompt_chose_local"
                new_state = SyncState.local_ahead
            elif choice.startswith("r"):
                resolution = "prompt_chose_remote"
                new_state = SyncState.remote_ahead
            else:
                resolution = "prompt_skipped"
                # Operator deferred — leave the mapping in `conflict`
                # so the next sync surfaces the same situation rather
                # than silently flipping to in_sync.
                new_state = SyncState.conflict
    elif strategy == ConflictResolutionStrategy.manual_merge:
        merge_path = _write_manual_merge_file(
            state_dir=state_dir, task=task, remote=remote
        )
        typer.echo(
            f"  conflict T={task.id}: manual_merge — wrote {merge_path}",
            err=True,
        )
        typer.echo(
            "  Resolve the file and delete it; rerun sync.",
            err=True,
        )
        _emit_audit(
            backend,
            action="sync.conflict_detected",
            payload={
                "provider_id": provider.provider_id,
                "task_id": task.id,
                "external_id": remote.external_id,
                "strategy": strategy_str,
                "resolution": "manual_merge_file_written",
                "audit_note": str(merge_path),
            },
            target_kind="task",
            target_id=task.id,
        )
        # SF-4: park the mapping in `conflict` and bump last_synced_at
        # so the next poll does NOT re-detect the same conflict (which
        # would re-write the merge file on every iteration). The
        # operator's resolution step is to delete the merge file and
        # rerun sync — which goes through this same code path.
        _bump_mapping_state(
            backend=backend,
            existing=existing,
            new_state=SyncState.conflict,
            clock_now=SystemClock().now(),
            actor=f"sync.{provider.provider_id}",
        )
        # Return resolved=False (don't raise) so the caller can continue
        # the batch — important for --watch mode where one task in
        # manual_merge must not halt the whole daemon. The batch loop
        # tracks the manual_merge count and exits 2 at the end if any
        # were pending. ``applied`` is False (no local Task mutation).
        return (False, False, "manual_merge_file_written")
    else:  # pragma: no cover — defensive; StrEnum is exhaustive above
        resolution = f"unknown_strategy:{strategy_str}"
        new_state = SyncState.conflict

    _emit_audit(
        backend,
        action="sync.conflict_detected",
        payload={
            "provider_id": provider.provider_id,
            "task_id": task.id,
            "external_id": remote.external_id,
            "strategy": strategy_str,
            "resolution": resolution,
        },
        target_kind="task",
        target_id=task.id,
    )
    # SF-4: bump mapping for every deferred resolution branch. Without
    # this the next --watch poll re-detects the same conflict because
    # last_synced_at never advances past the divergence point.
    _bump_mapping_state(
        backend=backend,
        existing=existing,
        new_state=new_state,
        clock_now=SystemClock().now(),
        actor=f"sync.{provider.provider_id}",
    )
    # T5 audit-honesty: every branch that falls through to here is
    # DEFERRED — no local Task mutation happened in this iteration. The
    # caller uses ``applied=False`` to emit ``sync.pull.deferred`` instead
    # of the historically-dishonest ``sync.pull.completed``. The future
    # immediate-apply variants (``local_wins_applied`` /
    # ``remote_wins_applied``) would set ``applied=True`` here and
    # perform the corresponding ``provider.push_task(...)`` /
    # ``_apply_remote_to_local(...)`` call BEFORE the bookkeeping bump.
    # See ``TODO(phase-9)`` markers on the local_wins / remote_wins
    # branches above; not wired in T5 because each variant needs its
    # own conflict-safety design (a re-push on local_wins can itself
    # race with a parallel remote edit, and the spec is not finalised).
    return (True, False, resolution)


def _write_manual_merge_file(
    *,
    state_dir: Path,
    task: Task,
    remote: ExternalTask,
) -> Path:
    """Write a markdown side-by-side merge document; return the path."""
    merge_dir = state_dir / _SYNC_CONFLICTS_DIRNAME
    merge_dir.mkdir(parents=True, exist_ok=True)
    target = merge_dir / f"{task.id}.md"
    body = (
        f"# Sync conflict for {task.id}\n\n"
        f"Resolve this file (edit local or accept remote), then DELETE it; "
        "rerun `anvil sync <provider>` to continue.\n\n"
        f"## Local (task {task.id})\n\n"
        f"- title: {task.title}\n"
        f"- status: {task.status}\n"
        f"- updated_at: {task.updated_at.isoformat()}\n\n"
        f"### description\n\n```\n{task.description}\n```\n\n"
        f"## Remote ({remote.external_id})\n\n"
        f"- title: {remote.title}\n"
        f"- status_label: {remote.status_label}\n"
        f"- last_modified: {remote.last_modified.isoformat()}\n\n"
        f"### body\n\n```\n{remote.body}\n```\n"
    )
    target.write_text(body, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------


def _run_watch_loop(
    *,
    backend: SqliteBackend,
    state_dir: Path,
    provider: SyncProvider,
    push: bool,
    pull: bool,
    fix: bool,
    task: str | None,
    yes: bool,
    interval: int,
    prd: str | None = None,
) -> None:
    """Poll forever until Ctrl-C. ``--interval 0`` runs ONE iteration.

    Single-iteration mode is the test seam: parallel-welder-territory-free
    way for the suite to exercise the watch path without sleeping.
    """
    stop_flag = {"stop": False}

    def _handler(signum: int, frame: Any) -> None:
        _ = (signum, frame)
        stop_flag["stop"] = True

    # Install SIGINT handler so Ctrl-C exits cleanly; restore on return.
    previous = signal.signal(signal.SIGINT, _handler)
    try:
        while not stop_flag["stop"]:
            # Each iteration is isolated: a single SyncProviderError, an
            # unexpected typer.Exit (e.g. from a per-task manual_merge),
            # or any other exception must NOT kill the daemon. Surface
            # the error on stderr and continue polling.
            try:
                _run_sync_once(
                    backend=backend,
                    state_dir=state_dir,
                    provider=provider,
                    push=push,
                    pull=pull,
                    fix=fix,
                    task=task,
                    yes=yes,
                    prd=prd,
                )
            except typer.Exit:
                # manual_merge etc. — surface but keep polling. The next
                # iteration will skip the same task (mapping is in conflict)
                # but advance the rest of the project.
                typer.echo(
                    "  watch: iteration aborted (operator action required); "
                    "next poll continues.",
                    err=True,
                )
            except Exception as exc:  # noqa: BLE001 — watch loop must survive
                typer.echo(
                    f"  watch: iteration failed ({type(exc).__name__}): {exc}; "
                    "next poll continues.",
                    err=True,
                )
            if interval <= 0:
                # Test seam: one iteration and out.
                break
            # Sleep in 1s slices so the SIGINT handler can wake us.
            slept = 0
            while slept < interval and not stop_flag["stop"]:
                time.sleep(1)
                slept += 1
    finally:
        signal.signal(signal.SIGINT, previous)


# ---------------------------------------------------------------------------
# Reconciliation helpers
# ---------------------------------------------------------------------------


def _resolve_configured_providers(state_dir: Path) -> list[str]:
    """Return the list of provider ids the reconciliation engine should scan.

    Phase 9 T5: honours the optional top-level ``sync.providers`` key in
    ``.anvil/config.yaml`` if present, otherwise falls back to
    ``sorted(PROVIDER_REGISTRY)`` (v1.8.0 behaviour: every registered
    provider counts).

    The config file is opened best-effort — if it cannot be read for any
    reason (missing, malformed, raises) the registry fallback is used so
    that ``anvil sync`` continues to work from a partially-broken
    project state. Validation errors at startup are the job of
    ``anvil init`` / ``anvil doctor``, not the sync
    reconciliation surface.

    An empty pinned list (``sync.providers: []``) is respected as-is:
    the operator explicitly opted out of every provider, so we return an
    empty list rather than silently falling back to the registry.
    """
    from anvil.sync import registry as sync_registry

    config_path = state_dir / "config.yaml"
    if config_path.exists():
        try:
            from anvil.config import load_config

            cfg = load_config(config_path)
            if cfg.sync_providers is not None:
                return list(cfg.sync_providers)
        except (ValueError, OSError, yaml.YAMLError):
            # Best-effort: defer the loud error to the next config-touching
            # command (init / doctor). Sync defaults to the registry so the
            # operator can still inspect/repair drift.
            # ``yaml.YAMLError`` covers malformed YAML — it is a subclass
            # of ``Exception`` (not of ``ValueError`` or ``OSError``), so
            # without it a syntactically broken config.yaml would escape
            # the best-effort catch and crash ``anvil sync`` with
            # an unhandled traceback. (Greptile P1, PR #50.)
            pass
    return sorted(sync_registry.PROVIDER_REGISTRY)


def _run_reconciliation(
    backend: SqliteBackend,
    state_dir: Path,
    project_root: Path | None = None,
) -> Any:
    """Build a ReconciliationEngine and run scan().

    ``project_root`` is the git checkout that plan-declared ``likely_files``
    resolve against (see :func:`_resolve_project_root`); it is distinct from
    ``state_dir`` under the HOME-workspace layout. ``None`` falls back to the
    legacy strip-``.anvil`` derivation for in-repo callers.
    """
    from anvil.clock import SystemClock

    # Configured providers list flows from the project's config (Phase 9
    # T5 ``sync.providers``) and falls back to the registry when absent.
    # Tests that monkeypatch ``PROVIDER_REGISTRY`` continue to work
    # because the fallback path queries the registry directly.
    from anvil.sync.reconciliation import ReconciliationEngine

    configured = _resolve_configured_providers(state_dir)
    engine = ReconciliationEngine(
        backend,
        state_dir=state_dir,
        clock=SystemClock(),
        configured_providers=configured,
        project_root=project_root,
    )
    return engine.scan()


def _apply_reconciliation_fixes(
    backend: SqliteBackend,
    state_dir: Path,
    report: Any,
    project_root: Path | None = None,
) -> list[Any]:
    """Build the engine again and call .fix() on the report."""
    from anvil.clock import SystemClock
    from anvil.sync.reconciliation import ReconciliationEngine

    configured = _resolve_configured_providers(state_dir)
    engine = ReconciliationEngine(
        backend,
        state_dir=state_dir,
        clock=SystemClock(),
        configured_providers=configured,
        project_root=project_root,
    )
    return engine.fix(report)


def _print_reconciliation_report(report: Any) -> None:
    """Render a reconciliation report to stdout."""
    typer.echo(f"Reconciliation scanned at {report.scanned_at.isoformat()}")
    if not report.discrepancies:
        typer.echo("  No discrepancies found.")
        return
    typer.echo(f"  Found {len(report.discrepancies)} discrepancy(ies):")
    for d in report.discrepancies:
        typer.echo(f"    [{d.severity}] {d.kind}: {d.target_id}")
        typer.echo(f"      {d.description}")
        typer.echo(f"      suggested: {d.suggested_fix}")
    typer.echo("")
    typer.echo("Summary:")
    for kind, count in sorted(report.summary.items()):
        typer.echo(f"  {kind}: {count}")


def _print_fix_actions(actions: list[Any]) -> None:
    """Render a list of FixAction to stdout."""
    if not actions:
        typer.echo("No fixes to apply.")
        return
    typer.echo("")
    typer.echo(f"Applied {len(actions)} fix(es):")
    for a in actions:
        marker = (
            "ok" if a.result == "applied"
            else "skip" if a.result == "skipped"
            else "FAIL"
        )
        line = f"  [{marker}] {a.kind} {a.target_id}: {a.command}"
        if a.error:
            line += f" — {a.error}"
        typer.echo(line)


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


def _emit_audit(
    backend: SqliteBackend,
    *,
    action: str,
    payload: dict[str, Any],
    target_kind: str,
    target_id: str,
) -> None:
    """Append a sync.* audit event via append().

    Strips None fields before dispatch.  After the Phase 9 T3 discriminated
    union, ``SyncAuditPayload`` is no longer a single all-optional model:
    each action has its own concrete subclass with action-specific REQUIRED
    fields (e.g. :class:`SyncPushFailedPayload` requires ``task_id``,
    ``exception_type``, ``exception_message``).  Callers MUST supply those
    required fields in ``payload`` — the None strip below does NOT excuse a
    missing required field.

    The strip remains load-bearing for OPTIONAL fields with ``None`` defaults
    (``external_id`` on first-push, ``audit_note`` on most events,
    ``resolution`` on clean pulls, etc.).  Without it the JSONL would carry
    ``"audit_note": null`` rows that clutter forensic queries and break
    ``jq 'has("audit_note")'`` filters.  The dispatcher in
    ``state/sqlite.py:_apply_mutation`` validates the cleaned dict against
    ``ACTION_TO_PAYLOAD[action]`` so any genuinely-missing REQUIRED field
    surfaces as ``ValidationError`` from ``append`` and propagates —
    silently dropping it would be the wrong fix.

    Audit emission failures are non-fatal: a sync that succeeded but
    whose audit row failed to write is strictly better than aborting
    the sync entirely. We catch only the specific failure classes the
    backend documents — anything else (KeyboardInterrupt, programmer
    errors like ValidationError, etc.) propagates so we don't silently
    swallow real bugs.
    """
    from anvil.clock import SystemClock
    from anvil.state.backend import EventRejected, StateLocked, TransactionAborted
    from anvil.state.models import EventDraft

    clean: dict[str, Any] = {k: v for k, v in payload.items() if v is not None}
    draft = EventDraft(
        timestamp=SystemClock().now(),
        actor="sync-cli",
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        payload_json=clean,
    )
    try:
        backend.append(draft)
    except (TransactionAborted, StateLocked, OSError) as exc:
        # Infra / contention: the sync itself already succeeded, so losing the
        # audit line is acceptable — warn and move on.
        typer.echo(
            f"  warning: failed to emit audit event {action!r}: "
            f"{type(exc).__name__}: {exc}",
            err=True,
        )
    except EventRejected as exc:
        # A rejected audit payload is a programmer error in THIS module's
        # payload construction (a malformed *_payload), not a user-input
        # problem — surface it loudly so a regression is not mistaken for
        # transient lock contention.
        typer.echo(
            f"  ERROR: audit event {action!r} rejected by validation "
            f"(malformed payload — this is a bug): {exc}",
            err=True,
        )


# ---------------------------------------------------------------------------
# tty detection
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    """Return True if stdin AND stdout are connected to an interactive terminal.

    typer.prompt reads from stdin (the question is answerable) and writes
    the prompt to stdout (the user can see it). If either side is
    redirected we cannot safely interact, so default to non-interactive
    behavior (caller falls back to local_wins with a stderr warning, or
    fails fast on --fix-without-yes).
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# JSON helper (kept here so tests can mock if needed)
# ---------------------------------------------------------------------------


def _to_json(obj: Any) -> str:  # pragma: no cover — convenience for debug printing
    return json.dumps(obj, sort_keys=True, default=str)
