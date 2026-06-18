"""``anvil scan`` — brownfield ingest of an existing repo (backlog T008).

Walks the existing working tree, persists a re-scannable *codebase model* in its
own SQLite db (``.anvil/scan.db``), and — on the first scan of a project
with no PRD yet — synthesises a draft ``prd.md`` plus an initial feature/task
graph by driving the same offline parse → plan → score → review pipeline that
``init --with-sample`` uses. Re-running ``scan`` reconciles against the persisted
model and reports the delta (added / removed / changed files) instead of
overwriting the seeded graph.

``init --from-repo`` is the convenience entry point: it scaffolds
``.anvil/`` (like a bare ``init``) and then immediately runs this scan.

Both surfaces honour the v1.24 conventions: ``ANVIL_ROOT`` resolution
via the shared helpers and a single ``--json`` envelope when ``--json`` is set.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from anvil.cli._helpers import (
    StateRootError,
    _open_backend,
    _resolve_base_dir,
    _resolve_state_dir,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail

if TYPE_CHECKING:
    from anvil.scan.model import CodebaseModel, ScanDelta

__all__ = ["scan"]

_COMMAND = "scan"
_PRD_FILENAME = "prd.md"


def scan(
    json_output: bool = JSON_OPTION,
    force: bool = typer.Option(  # noqa: B008
        False,
        "--force",
        help=(
            "Re-seed the draft PRD and task graph even when a PRD already "
            "exists. Without this, a re-scan only updates the codebase model "
            "and reports the file delta — it never clobbers an authored PRD."
        ),
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Scan the working tree, persist a codebase model, and seed a draft graph.

    First run (no PRD yet): writes ``.anvil/prd.md`` from the discovered
    structure and seeds features/tasks so ``anvil next`` returns a ready
    task. Subsequent runs: refresh the persisted codebase model and report the
    added / removed / changed file delta. Pass ``--force`` to re-seed the PRD.
    """
    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError as exc:
        if json_output:
            fail(_COMMAND, str(exc), code="state_root_invalid")
        raise

    if not state_dir.exists():
        msg = (
            "anvil not initialized in this project. Run "
            "`anvil init` (or `anvil init --from-repo`) first."
        )
        if json_output:
            fail(_COMMAND, msg, code="not_initialized")
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(code=1)

    project_root = _resolve_base_dir(cwd)
    result = _run_scan(state_dir, project_root, force=force)

    if json_output:
        emit_success(_COMMAND, result)
        return

    _print_human(result, state_dir)


# ---------------------------------------------------------------------------
# Core scan logic (shared by `scan` and `init --from-repo`)
# ---------------------------------------------------------------------------


def run_scan_and_report(
    state_dir: Path,
    project_root: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Public entry point used by both ``scan`` and ``init --from-repo``.

    Returns the same ``data`` dict the ``--json`` envelope carries.
    """
    return _run_scan(state_dir, project_root, force=force)


def _run_scan(
    state_dir: Path,
    project_root: Path,
    *,
    force: bool,
) -> dict[str, Any]:
    from anvil.scan.model import (
        SCAN_DB_NAME,
        compute_delta,
        load_model,
        save_model,
        scan_working_tree,
    )

    scan_db = state_dir / SCAN_DB_NAME

    previous: CodebaseModel | None = load_model(scan_db)
    current: CodebaseModel = scan_working_tree(project_root)
    delta: ScanDelta = compute_delta(previous, current)

    # Persist the refreshed model AFTER computing the delta against the old one.
    save_model(current, scan_db)

    seeded: dict[str, Any] | None = None
    prd_path = state_dir / _PRD_FILENAME
    seed_reason = _should_seed(state_dir, prd_path, force=force)
    if seed_reason is not None:
        seeded = _seed_draft(state_dir, project_root, current)

    return {
        "project_root": str(project_root),
        "scan_db": str(scan_db),
        "files_scanned": current.file_count,
        "components": {
            name: len(files) for name, files in current.components().items()
        },
        "languages": current.language_counts(),
        "first_scan": previous is None,
        "delta": {
            "added": delta.added,
            "removed": delta.removed,
            "changed": delta.changed,
            "unchanged_count": len(delta.unchanged),
        },
        "seeded": seeded,
        "prd_path": str(prd_path) if seeded is not None else None,
    }


def _should_seed(state_dir: Path, prd_path: Path, *, force: bool) -> str | None:
    """Return a reason string when the draft graph should be (re)seeded, else None.

    Seeds when the project has no PRD yet (the common brownfield first run), or
    when ``--force`` is given. Never overwrites an authored/approved PRD without
    ``--force`` — a re-scan of an active project just refreshes the model.
    """
    if force:
        return "force"

    # If a PRD already exists in state, do not re-seed (idempotent re-scan).
    backend = _open_backend(state_dir)
    try:
        prd = backend.get_prd()
    finally:
        backend.close()
    if prd is not None:
        return None
    if prd_path.exists():
        # A prd.md exists but was never parsed into state — leave it for the
        # user to `prd parse` rather than clobbering their draft.
        return None
    return "no_prd"


def _seed_draft(
    state_dir: Path,
    project_root: Path,
    model: CodebaseModel,
) -> dict[str, Any]:
    """Write the draft prd.md and drive the offline seed pipeline.

    Reuses ``cli._sample.seed_pipeline_from_prd`` (the same engine path
    ``init --with-sample`` uses) so the brownfield seed cannot drift from the
    hand-run command sequence.
    """
    from anvil.cli._sample import (
        SampleSeedError,
        seed_pipeline_from_prd,
    )
    from anvil.scan.prd_draft import draft_prd_from_model

    project_name = _project_name(state_dir, project_root)
    prd_text = draft_prd_from_model(model, project_name=project_name)
    (state_dir / _PRD_FILENAME).write_text(prd_text, encoding="utf-8")

    backend = _open_backend(state_dir)
    try:
        summary = seed_pipeline_from_prd(
            backend,
            prd_text,
            actor="anvil-cli",
            review_notes="auto-seeded by scan (brownfield)",
            parse_error_hint=(
                "The generated draft PRD failed to parse — this is a "
                "anvil bug; please report it."
            ),
        )
    except SampleSeedError as exc:
        # A draft we generated should always parse; if it does not, surface a
        # clean error rather than a traceback (close() is idempotent, so the
        # finally below still runs).
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        backend.close()
    return summary


def _project_name(state_dir: Path, project_root: Path) -> str:
    """Resolve a human-readable project name from config, else the dir name."""
    config_path = state_dir / "config.yaml"
    if config_path.exists():
        try:
            from anvil.config import load_config

            return load_config(config_path).project_name
        except Exception:  # noqa: BLE001
            pass
    return project_root.name


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------


def _print_human(result: dict[str, Any], state_dir: Path) -> None:
    typer.echo(f"Scanned {result['files_scanned']} file(s) from {result['project_root']}")
    components = result["components"]
    if components:
        typer.echo("")
        typer.echo("Components:")
        for name, count in components.items():
            typer.echo(f"  {name}: {count} file(s)")
    languages = result["languages"]
    if languages:
        typer.echo("")
        typer.echo("Languages:")
        for lang, count in languages.items():
            typer.echo(f"  {lang}: {count}")

    delta = result["delta"]
    typer.echo("")
    if result["first_scan"]:
        typer.echo("First scan — persisted a new codebase model.")
    else:
        typer.echo(
            "Re-scan delta: "
            f"{len(delta['added'])} added, "
            f"{len(delta['removed'])} removed, "
            f"{len(delta['changed'])} changed, "
            f"{delta['unchanged_count']} unchanged."
        )
        for path in delta["added"]:
            typer.echo(f"  + {path}")
        for path in delta["removed"]:
            typer.echo(f"  - {path}")
        for path in delta["changed"]:
            typer.echo(f"  ~ {path}")

    seeded = result["seeded"]
    if seeded is not None:
        typer.echo("")
        typer.echo(
            "Seeded draft project: "
            f"{seeded['features']} feature(s), "
            f"{seeded['tasks']} task(s), "
            f"{seeded['ready']} ready."
        )
        typer.echo(f"  {result['prd_path']}")
        typer.echo("")
        typer.echo(
            "Draft PRD is a SEED — edit it to capture real intent, then run "
            "`anvil next` to see your first ready task."
        )
    else:
        typer.echo("")
        typer.echo("Codebase model refreshed (PRD left unchanged).")
