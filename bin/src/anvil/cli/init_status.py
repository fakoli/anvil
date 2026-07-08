"""init and status commands (Phase 2)."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import TYPE_CHECKING

import click
import typer

from anvil.cli._helpers import (
    _STATE_DIR_NAME,
    PRD_OPTION,
    _is_local_layout,
    _is_plugin_root,
    _open_backend,
    _resolve_base_dir,
    _resolve_state_dir,
    _slug,
    canonical_prd_id,
    resolve_prd_id,
)
from anvil.cli._json import JSON_OPTION, dump_model, emit_success, fail
from anvil.state.rollup import compute_prd_rollup

if TYPE_CHECKING:
    from anvil.state.sqlite import SqliteBackend


# ---------------------------------------------------------------------------
# init subcommand
# ---------------------------------------------------------------------------


def _suggest_project_dir(plugin_root: Path) -> str | None:
    """Return the name of an immediate sub-dir that holds a ``pyproject.toml``.

    When `init` is refused at the plugin root, this names a concrete project dir
    to point the user at (e.g. ``bin`` for anvil's own repo) instead of leaving
    them to guess (B29). Returns None when no such sibling exists.
    """
    for pyproject in sorted(plugin_root.glob("*/pyproject.toml")):
        return pyproject.parent.name
    return None


def init(
    name: str | None = typer.Option(  # noqa: B008
        None,
        "--name",
        help=(
            "Human-readable project name. "
            "Defaults to the basename of the current directory."
        ),
    ),
    id: str | None = typer.Option(  # noqa: A002,B008
        None,
        "--id",
        help=(
            "Project identifier slug (e.g. 'my-project'). "
            "Defaults to a slug derived from --name."
        ),
    ),
    force: bool = typer.Option(  # noqa: B008
        False,
        "--force",
        help="Overwrite an existing .anvil/ directory.",
    ),
    with_sample: bool = typer.Option(  # noqa: B008
        False,
        "--with-sample",
        help=(
            "Seed a runnable sample project: write a valid sample prd.md and "
            "run parse, plan, and score offline so `anvil next` returns "
            "a ready task with no further input. Requires no LLM / API key. "
            "Without this flag, init behaviour is unchanged."
        ),
    ),
    from_repo: bool = typer.Option(  # noqa: B008
        False,
        "--from-repo",
        help=(
            "Brownfield ingest: after scaffolding, scan the existing working "
            "tree (T008) to persist a re-scannable codebase model, write a "
            "draft prd.md, and seed an initial feature/task graph offline. "
            "Mutually exclusive with --with-sample. Without this flag, init "
            "behaviour is unchanged."
        ),
    ),
) -> None:
    """Scaffold a .anvil/ directory in the current working directory.

    Creates the canonical project-state layout including config.yaml,
    state.db (SQLite), events.jsonl (append-only event log), and an
    empty packets/ subdirectory.

    With ``--with-sample`` the scaffold is followed by a one-command
    quickstart: a self-contained sample ``prd.md`` is written and the full
    deterministic pipeline (parse → review → approve → plan → score →
    review tasks) is run offline, leaving at least one task in ``ready`` so
    ``anvil next`` works immediately.
    """
    from anvil.config import write_default_config

    # --from-repo and --with-sample both own prd.md and seed the task graph;
    # running both would double-seed. Refuse the combination up front.
    if from_repo and with_sample:
        typer.echo(
            "Error: --from-repo and --with-sample are mutually exclusive. "
            "Use --from-repo to ingest the existing repo, or --with-sample for "
            "the toy quickstart.",
            err=True,
        )
        raise typer.Exit(code=1)

    # MUST-FIX 1: resolve the project root the SAME way reads do
    # (ANVIL_ROOT > cwd) so `init` and `status` never diverge. `init`
    # has no --cwd flag, so we pass None and let the env var (or cwd) decide.
    cwd = _resolve_base_dir(None)

    # Guard: refuse to initialise inside the anvil plugin directory — but ONLY
    # under the legacy local layout, where state would be scaffolded in-repo. In
    # the default workspace layout init writes to ~/.anvil/... (never into the
    # repo), so initialising "at the plugin root" is harmless and in fact correct
    # for anvil-on-anvil dogfooding (B44: the guard was dead in workspace layout —
    # it checked the resolved HOME base, never a plugin root).
    if _is_local_layout() and _is_plugin_root(cwd):
        suggestion = _suggest_project_dir(cwd)
        lines = [
            "Error: this directory is the anvil plugin root. "
            "Run `anvil init` from your project directory, not from inside the plugin.",
        ]
        if suggestion is not None:
            lines.append(
                f"  To manage anvil's own work, run: cd '{suggestion}' && anvil init"
            )
        lines.append("  Or set ANVIL_ROOT=<project-dir> to point anvil at it.")
        typer.echo("\n".join(lines), err=True)
        raise typer.Exit(code=1)

    state_dir = cwd / _STATE_DIR_NAME

    # Guard: existing state directory without --force.
    if state_dir.exists() and not force:
        typer.echo(
            f"Error: {state_dir} already exists. "
            "Pass --force to reinitialise.",
            err=True,
        )
        raise typer.Exit(code=1)

    # --force reinit: wipe the canonical state files before scaffolding so the
    # replay/audit guarantee holds. Without this, the new project.created and
    # state.initialized events would be appended to the old events.jsonl,
    # producing duplicate IDs and a log that no longer replays to current DB.
    # packets/ is preserved (user-generated work packets are not canonical
    # state). snapshots/ may exist if `anvil snapshot` was run; if
    # present it is also preserved for the same reason (PS-2: init no longer
    # pre-creates it).
    if state_dir.exists() and force:
        db_file = state_dir / "state.db"
        if db_file.exists():
            db_file.unlink()
        # WAL/SHM sidecar files left by SQLite must go too.
        for sidecar in ("state.db-wal", "state.db-shm"):
            sidecar_path = state_dir / sidecar
            if sidecar_path.exists():
                sidecar_path.unlink()
        events_file = state_dir / "events.jsonl"
        if events_file.exists():
            events_file.unlink()

    # Resolve project name and id.
    project_name = name if name else cwd.name
    project_id = id if id else _slug(project_name)

    # Create directory structure.
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "packets").mkdir(exist_ok=True)
    (state_dir / "events.jsonl").touch()
    # Note: snapshots/ used to be pre-created here, but nothing in the
    # codebase ever writes to it (PS-2). When `anvil snapshot` ships
    # it will create the directory on first use.

    # Write config.yaml via the config module.
    # write_default_config generates a UUID for project_id internally; the --id
    # argument controls the project_id used in the state backend event below.
    config_path = state_dir / "config.yaml"
    if config_path.exists() and force:
        config_path.unlink()
    write_default_config(config_path, project_name=project_name)

    # Initialise state.db via SqliteBackend. When --with-sample is set, seed
    # the full PRD→ready pipeline within the SAME backend session so we don't
    # re-open the db and so the sample run is atomic with init.
    seed_summary: dict[str, object] | None = None
    backend = _open_backend(state_dir)
    try:
        _apply_init_event(backend, project_name=project_name, project_id=project_id)

        if with_sample:
            from anvil.cli._sample import (
                SampleSeedError,
                seed_sample_pipeline,
                write_sample_prd,
            )

            write_sample_prd(state_dir)
            try:
                seed_summary = seed_sample_pipeline(backend)
            except SampleSeedError as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
    finally:
        backend.close()

    # Print confirmation.
    typer.echo(f"Initialized anvil for '{project_name}' (id: {project_id})")
    typer.echo("")
    typer.echo(f"  {config_path}")
    typer.echo(f"  {state_dir / 'state.db'}")
    typer.echo(f"  {state_dir / 'events.jsonl'}")
    typer.echo(f"  {state_dir / 'packets'}/")

    if seed_summary is not None:
        # --with-sample: report the seeded pipeline and point straight at
        # `next` (the whole reason for this flag is zero-to-next with no
        # further input).
        typer.echo(f"  {state_dir / 'prd.md'}")
        typer.echo("")
        typer.echo(
            "Seeded sample project: "
            f"{seed_summary['features']} feature(s), "
            f"{seed_summary['tasks']} task(s), "
            f"{seed_summary['ready']} ready."
        )
        typer.echo("")
        typer.echo("Next step: run `anvil next` to see your first ready task.")
    elif from_repo:
        # Brownfield ingest (T008): scaffold is done; now scan the existing
        # working tree, persist the codebase model, write a draft prd.md, and
        # seed the feature/task graph. Reuses the scan engine so init and the
        # standalone `scan` command stay in lock-step.
        from anvil.cli.scan import run_scan_and_report

        typer.echo("")
        result = run_scan_and_report(state_dir, cwd, force=False)
        seeded = result.get("seeded")
        typer.echo(
            f"Scanned {result['files_scanned']} file(s) into a codebase model."
        )
        if seeded is not None:
            typer.echo(f"  {state_dir / 'prd.md'}")
            typer.echo("")
            typer.echo(
                "Seeded draft project from repo: "
                f"{seeded['features']} feature(s), "
                f"{seeded['tasks']} task(s), "
                f"{seeded['ready']} ready."
            )
            typer.echo("")
            typer.echo(
                "Draft PRD is a SEED — edit it to capture real intent, then "
                "run `anvil next` to see your first ready task."
            )
        else:
            typer.echo("")
            typer.echo(
                "Next step: author your PRD at "
                f"{state_dir / 'prd.md'}, "
                "then run `anvil prd parse`."
            )
    else:
        typer.echo("")
        typer.echo(
            "Next step: author your PRD at "
            f"{state_dir / 'prd.md'}, "
            "then run `anvil prd parse`."
        )
        # GAP-02: the parser requires four sections and a specific bold-inline
        # field format. State them here so the first `prd parse` doesn't fail
        # blind on a missing heading or a mis-formatted feature/task field.
        typer.echo("")
        typer.echo(
            "Your prd.md must contain these required sections:\n"
            "  # Project: <Name>\n"
            "  ## Summary\n"
            "  ## Goals\n"
            "  ## Requirements\n"
            "Optional ## Features / ## Tasks use bold-inline fields, e.g.\n"
            "  **Feature:** F001   (under a ### Txxx task heading)\n"
            "  **Requirements:** R001, R002   (under a ### Fxxx feature heading)\n"
            "See docs/prd-template.md for the full template."
        )


def _apply_init_event(
    backend: SqliteBackend,
    *,
    project_name: str,
    project_id: str,
) -> None:
    """Build and apply the project.created and state.initialized events.

    These two events seed the project row in state.db and mark the
    initialisation in the append-only audit log.
    """
    from anvil.clock import SystemClock
    from anvil.state.models import EventDraft

    clock = SystemClock()
    now = clock.now()

    project_draft = EventDraft(
        timestamp=now,
        actor="anvil-cli",
        action="project.created",
        target_kind="project",
        target_id=project_id,
        payload_json={
            "id": project_id,
            "name": project_name,
            "description": "",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
    )
    backend.append(project_draft)

    init_draft = EventDraft(
        timestamp=now,
        actor="anvil-cli",
        action="state.initialized",
        target_kind="project",
        target_id=project_id,
        payload_json={},
    )
    backend.append(init_draft)


# ---------------------------------------------------------------------------
# status subcommand
# ---------------------------------------------------------------------------


def status(
    hook_format: bool = typer.Option(  # noqa: B008
        False,
        "--hook-format",
        help=(
            "Print a single compact line for SessionStart hook consumption. "
            "Exits 0 even when anvil is not initialized "
            "(hooks must never fail the session)."
        ),
    ),
    prd: str | None = PRD_OPTION,
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help=(
            "Project directory to inspect. "
            "Defaults to the current working directory."
        ),
    ),
) -> None:
    """Show the current anvil summary for this project.

    Default output is a human-readable multi-line summary.
    Pass --hook-format for the single-line compact format consumed by
    the SessionStart detect-state.sh hook.
    With ``--json`` emits ``{"ok": true, "command": "status", "data": {...}}``
    carrying project, prd status, task counts, and active claim count; the
    not-initialized case is a ``{"ok": false, ...}`` envelope with exit 1.
    """
    # MUST-FIX 3 (hook safety): in --hook-format mode the SessionStart hook must
    # NEVER fail the session. _resolve_state_dir can raise StateRootError when
    # ANVIL_ROOT is set but invalid; swallow it here and emit the benign
    # "uninitialized" line with exit 0 instead of propagating the error.
    from anvil.cli._helpers import StateRootError

    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError:
        if hook_format:
            typer.echo("uninitialized")
            raise typer.Exit(code=0) from None
        raise

    if not state_dir.exists():
        if json_output:
            fail(
                "status",
                "anvil not initialized in this project. "
                "Run `anvil init` to start.",
                code="not_initialized",
            )
        if hook_format:
            typer.echo("uninitialized")
            raise typer.Exit(code=0)
        typer.echo(
            "anvil not initialized in this project. "
            "Run `anvil init` to start."
        )
        raise typer.Exit(code=1)

    from anvil.state.backend import SchemaMismatch
    from anvil.state.schema import get_schema_version
    from anvil.state.sqlite import read_db_schema_version

    schema_version = get_schema_version()

    # T007/B11 (MUST-FIX 2a): read the TRUE on-disk PRAGMA user_version BEFORE
    # the backend opens (open() migrates v0-v3 up and re-stamps it, masking
    # real drift). This standalone read makes drift (db < code) observable.
    db_schema_version = read_db_schema_version(str(state_dir / "state.db"))

    # MUST-FIX 2: an un-migratable / unknown schema version (e.g. a db stamped
    # user_version=99) makes _open_backend() raise SchemaMismatch. status must
    # report that through the normal error path — a clean "Error: ..." line
    # (exit 1) or a {"ok": false, ...} envelope — NEVER a raw traceback.
    try:
        backend = _open_backend(state_dir)
    except SchemaMismatch as exc:
        if json_output:
            fail("status", str(exc), code="schema_mismatch")
        if hook_format:
            # Hook safety: never fail the session on a schema problem.
            typer.echo("uninitialized")
            raise typer.Exit(code=0) from None
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    try:
        project = backend.get_project()
        # T021 audit (get_prd no-arg): default-only-correct. The flat
        # ``prd_status`` line (and the --hook-format ``prd-status`` token) is the
        # legacy single-PRD summary — the default PRD's status. The per-PRD
        # rollup below (compute_prd_rollup over list_prds()) scopes status to
        # each partition; the flat field stays pinned to the default PRD.
        # Hook output is a project-level compatibility surface consumed by
        # SessionStart hooks; do not let ANVIL_PRD silently change its shape.
        # Still honor an explicit `status --hook-format --prd X` request.
        ctx = click.get_current_context(silent=True)
        prd_source = ctx.get_parameter_source("prd") if ctx is not None else None
        status_prd = (
            None
            if hook_format and prd_source is click.core.ParameterSource.ENVIRONMENT
            else prd
        )
        scoped_prd_id = (
            canonical_prd_id(resolve_prd_id(backend, status_prd))
            if status_prd
            else None
        )
        if scoped_prd_id is None:
            prd_model = backend.get_prd()
            prds = backend.list_prds()
            all_tasks = backend.list_tasks()
            active_claims = backend.list_active_claims()
        else:
            prd_model = backend.get_prd(scoped_prd_id)
            prds = [prd_model] if prd_model is not None else []
            all_tasks = backend.list_tasks(prd_id=scoped_prd_id)
            scoped_task_ids = {task.id for task in all_tasks}
            active_claims = [
                claim
                for claim in backend.list_active_claims()
                if claim.task_id in scoped_task_ids
            ]

        # retro-opps T012 — heartbeat-bus read-back: latest progress.noted
        # phase + elapsed + time-to-lease-expiry per active claim. Computed
        # while the backend is open; best-effort (a read hiccup on one claim
        # must not break status).
        from anvil.clock import SystemClock as _SystemClock

        _now = _SystemClock().now()
        claim_details: list[dict[str, object]] = []
        for _claim in active_claims:
            try:
                _latest = backend.latest_event_payload(
                    _claim.task_id, "progress.noted"
                )
                # Review finding: only attribute a phase recorded during THIS
                # claim's lifetime — audit rows are append-only, so a
                # re-claimed task keeps prior progress.noted events and would
                # otherwise pair a fresh elapsed=0m with a stale phase.
                _phase = None
                if _latest is not None:
                    _evt_ts = datetime.datetime.fromisoformat(_latest[1])
                    if _evt_ts.tzinfo is None:
                        _evt_ts = _evt_ts.replace(tzinfo=datetime.UTC)
                    if _evt_ts >= _claim.created_at:
                        _phase = _latest[0].get("phase")
            except Exception:  # noqa: BLE001 — read-back is best-effort
                _phase = None
            claim_details.append(
                {
                    "claim_id": _claim.id,
                    "task_id": _claim.task_id,
                    "actor": _claim.claimed_by,
                    "phase": _phase,
                    "elapsed_seconds": int(
                        (_now - _claim.created_at).total_seconds()
                    ),
                    "lease_expires_in_seconds": int(
                        (_claim.lease_expires_at - _now).total_seconds()
                    ),
                }
            )
    finally:
        backend.close()

    # Aggregate task counts. claimed / needs_review / done are surfaced too —
    # the 0.3.0 rollup hid them, so mid-loop `status` showed a claimed task in
    # no bucket and a finished project as "0 of N" (found reproducing the
    # README flow: counts must agree with the packet's documented lifecycle).
    ready_count = sum(1 for t in all_tasks if t.status == "ready")
    claimed_count = sum(1 for t in all_tasks if t.status == "claimed")
    in_progress_count = sum(1 for t in all_tasks if t.status == "in_progress")
    needs_review_count = sum(1 for t in all_tasks if t.status == "needs_review")
    blocked_count = sum(1 for t in all_tasks if t.status == "blocked")
    done_count = sum(1 for t in all_tasks if t.status == "done")

    prd_status_str = str(prd_model.status) if prd_model is not None else "none"
    active_claim_count = len(active_claims)

    # T020: per-PRD rollup. The flat fields above stay the PROJECT TOTAL; the
    # rollup adds one slice per PRD. On a single-PRD DB the one entry's numbers
    # equal those totals (see compute_prd_rollup).
    rollup = compute_prd_rollup(prds, all_tasks, active_claims)

    if json_output:
        emit_success(
            "status",
            {
                "project": dump_model(project) if project is not None else None,
                "prd_status": prd_status_str,
                "tasks": {
                    "total": len(all_tasks),
                    "ready": ready_count,
                    "claimed": claimed_count,
                    "in_progress": in_progress_count,
                    "needs_review": needs_review_count,
                    "blocked": blocked_count,
                    "done": done_count,
                },
                "active_claims": active_claim_count,
                # retro-opps T012 — per-claim heartbeat-bus read-back
                # (additive; active_claims stays the count for compat).
                "claims": claim_details,
                # T020: additive per-PRD rollup alongside the flat project totals.
                "prds": [dump_model(entry) for entry in rollup],
                # T007/B11: code-targeted schema version (== SCHEMA_VERSION),
                # plus the version stamped on this DB for drift detection.
                "schema_version": schema_version,
                "db_schema_version": db_schema_version,
            },
        )
        return

    if hook_format:
        line = (
            f"active-claims:{active_claim_count} "
            f"ready-tasks:{ready_count} "
            f"blockers:{blocked_count} "
            f"prd-status:{prd_status_str}"
        )
        typer.echo(line)
        raise typer.Exit(code=0)

    # Human-readable multi-line output.
    project_name = project.name if project is not None else "(unknown)"
    project_id_str = project.id if project is not None else "(unknown)"
    config_path = state_dir / "config.yaml"

    # Try to read project metadata from config if backend has no project row.
    if project is None and config_path.exists():
        try:
            from anvil.config import load_config

            cfg = load_config(config_path)
            project_name = cfg.project_name
            project_id_str = cfg.project_id
        except Exception:  # noqa: BLE001  (config errors must not crash status)
            pass

    # Determine initialized-at timestamp from the first events.jsonl entry.
    initialized_at = _read_initialized_at(state_dir)

    sync_label = "off"
    if config_path.exists():
        try:
            from anvil.config import load_config

            cfg = load_config(config_path)
            if cfg.sync_github_enabled:
                sync_label = "github"
        except Exception:  # noqa: BLE001
            pass

    typer.echo(f'anvil for "{project_name}" (id: {project_id_str})')
    typer.echo(f"Path: {state_dir}")
    typer.echo(f"Initialized: {initialized_at}")

    # T020: one block per PRD (id, status, counts, ready, active claims). On a
    # single-PRD DB this is exactly one block whose numbers equal the PROJECT
    # TOTAL printed below.
    for entry in rollup:
        typer.echo("")
        typer.echo(f"PRD {entry.prd_id} ({entry.status})")
        typer.echo(
            f"  Tasks:         {entry.total_tasks} total "
            f"({entry.ready_task_count} ready, "
            f"{entry.task_counts.get('claimed', 0)} claimed, "
            f"{entry.task_counts.get('in_progress', 0)} in_progress, "
            f"{entry.task_counts.get('needs_review', 0)} needs_review, "
            f"{entry.task_counts.get('blocked', 0)} blocked, "
            f"{entry.task_counts.get('done', 0)} done)"
        )
        typer.echo(f"  Active claims: {entry.active_claim_count}")

    typer.echo("")
    typer.echo("PROJECT TOTAL")
    typer.echo(f"PRD:           {prd_status_str}")
    typer.echo(
        f"Tasks:         {len(all_tasks)} total "
        f"({ready_count} ready, "
        f"{claimed_count} claimed, "
        f"{in_progress_count} in_progress, "
        f"{needs_review_count} needs_review, "
        f"{blocked_count} blocked, "
        f"{done_count} done)"
    )
    typer.echo(f"Active claims: {active_claim_count}")
    # retro-opps T012 — one line per active claim: latest phase, elapsed since
    # claim, minutes to lease expiry. Placeholder phase when no progress event
    # exists yet. (--hook-format exits above; its single line is untouched.)
    for detail in claim_details:
        elapsed_min = detail["elapsed_seconds"] // 60
        expires_seconds = detail["lease_expires_in_seconds"]
        # Expired-but-unreaped claims read clearer than "-3m" (JSON keeps the
        # raw negative int — honest for machine consumers).
        expiry_label = (
            f"lease-expires-in={expires_seconds // 60}m"
            if expires_seconds >= 0
            else "lease=EXPIRED (unreaped)"
        )
        phase_label = detail["phase"] or "-"
        typer.echo(
            f"  {detail['task_id']} [{detail['actor']}] "
            f"phase={phase_label} elapsed={elapsed_min}m {expiry_label}"
        )
    typer.echo(f"Sync:          {sync_label}")
    # T007/B11 (MUST-FIX 2a): db_schema_version is the TRUE on-disk version read
    # BEFORE this status call opened (and thereby auto-migrated) the db. When it
    # is BEHIND the code-targeted version, surface the drift so the user knows a
    # migration just ran (v0-v3 auto-upgrade on open); otherwise show the single
    # matching number.
    if db_schema_version != schema_version:
        typer.echo(
            f"Schema:        {schema_version} "
            f"(db was v{db_schema_version}, migrated on open)"
        )
    else:
        typer.echo(f"Schema:        {schema_version}")


def _read_initialized_at(state_dir: Path) -> str:
    """Return the ISO timestamp from the first events.jsonl entry.

    Falls back to the mtime of state.db, then to 'unknown'.
    """
    events_path = state_dir / "events.jsonl"
    if events_path.exists():
        try:
            with events_path.open(encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    ts = data.get("timestamp", "")
                    if ts:
                        return str(ts)
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    db_path = state_dir / "state.db"
    if db_path.exists():
        try:
            mtime = db_path.stat().st_mtime
            dt = datetime.datetime.fromtimestamp(mtime, tz=datetime.UTC)
            return dt.isoformat()
        except OSError:
            pass

    return "unknown"
