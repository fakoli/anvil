"""``anvil migrate-workspace`` — one-time migration of legacy in-repo state into
the HOME workspace (B44).

Before the home-workspace default (#42), anvil kept state in-repo at
``<repo>/.anvil`` (or ``<repo>/bin/.anvil`` for the anvil-on-anvil dogfooding
case). This verb copies that legacy state into the canonical home workspace
(``~/.anvil/workspaces/<key>/.anvil``) so a project that predates #42 resolves
its history under the new layout.

SAFE-FIRST (the #1 invariant — state is never lost or clobbered):

* **Dry-run by default** — reports source → target and exits 0 writing nothing;
  re-run with ``--yes`` to apply.
* **No-clobber** — if a home workspace already exists for this project, it is
  authoritative; the verb skips (never overwrites it).
* **Copy, never move** — the legacy dir is left untouched as a fallback.
* **Atomic** — copies into a temp sibling then ``os.replace``s it into place, so
  an interrupted copy can never leave a half-populated workspace.
* **Whole-tree** — copies the entire ``.anvil/`` (state.db + ``-wal``/``-shm``
  sidecars, events.jsonl, config, prd, packets/, .evidence-buffer/).

``--adopt-legacy`` (B44-1) is a separate, explicit binding step for the other
kind of legacy state: a BARE-NAME home workspace (``~/.anvil/workspaces/<name>``)
left by the original #42 code. Such a directory is keyed on the basename
alone, so anvil no longer honours it for whichever same-named checkout asks
first. Adoption writes an owner marker naming THIS project's canonical root —
dry-run by default, ``--yes`` to apply, and it copies or moves nothing.
"""

from __future__ import annotations

import datetime
import os
import shutil
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import typer

from anvil.cli._helpers import (
    _STATE_DIR_NAME,
    WORKSPACE_OWNER_MARKER,
    _canonical_project_root,
    _home_workspace_base,
    _legacy_workspace_base,
    _workspace_key,
    read_workspace_owner,
    unclaimed_legacy_workspace,
    workspace_owner_matches,
    write_workspace_owner,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail
from anvil.state.backend import SchemaProbeFailed
from anvil.state.file_inspection import BoundedFileInspection
from anvil.state.sqlite import (
    _require_current_state_lock,
    _require_no_event_recovery,
    _schema_initialization_lock,
    _state_operation_lock,
)

__all__ = ["migrate_workspace"]

_COMMAND = "migrate-workspace"


def _display_path(path: Path) -> str:
    """Return a stable CLI/JSON path spelling across Windows and POSIX."""
    return path.as_posix()


def migrate_workspace(
    yes: bool = typer.Option(  # noqa: B008
        False,
        "--yes",
        help="Apply the migration. Without this it is a dry run (reports, writes nothing).",
    ),
    adopt_legacy: bool = typer.Option(  # noqa: B008
        False,
        "--adopt-legacy",
        help=(
            "Bind an unclaimed legacy bare-name home workspace "
            "(~/.anvil/workspaces/<project-name>) to this project instead of "
            "copying in-repo state. Dry run unless --yes is also given."
        ),
    ),
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Migrate legacy in-repo ``.anvil/`` state into the HOME workspace (B44).

    Dry-run by default (re-run with ``--yes``). Never clobbers an existing home
    workspace, and copies (never moves) so the legacy dir survives as a fallback.

    With ``--adopt-legacy``, bind an unclaimed bare-name home workspace to this
    project instead (also dry-run by default).
    """
    loc = cwd or Path.cwd()
    root = _canonical_project_root(loc)
    if adopt_legacy:
        _adopt_legacy_workspace(loc, root, yes=yes, json_output=json_output)
        return
    target_base = _home_workspace_base(loc)
    target_anvil = target_base / ".anvil"
    # A bare-name workspace is never honoured implicitly; make it discoverable
    # from the verb a pre-#42 user is most likely to reach for.
    adoptable = unclaimed_legacy_workspace(loc)

    def _report(json_flag: bool, data: dict[str, Any]) -> None:
        if adoptable is not None:
            data = {
                **data,
                "adoptable_legacy_workspace": _display_path(adoptable),
                "message": (
                    f"{data['message']} Note: an unclaimed legacy workspace exists at "
                    f"{_display_path(adoptable)}; run `anvil migrate-workspace "
                    "--adopt-legacy` if it belongs to this project."
                ),
            }
        _emit_report(json_flag, data)

    # No-clobber: an existing home workspace is authoritative — skip entirely.
    if target_anvil.exists():
        _report(json_output, {
            "status": "already_migrated",
            "source": None,
            "target": _display_path(target_anvil),
            "applied": False,
            "message": (
                f"Home workspace already exists at {_display_path(target_anvil)}; "
                "nothing to migrate "
                f"(it is authoritative — anvil never overwrites it)."
            ),
        })
        return

    # Detect a legacy source, first with a state.db: probe the actual location
    # (which may be a non-main worktree with its OWN stranded .anvil) AND the
    # canonical repo root, each at .anvil then bin/.anvil. Dedup so loc==root
    # isn't probed twice.
    legacy_src: Path | None = None
    seen: set[Path] = set()
    for base in (loc.resolve(), root):
        for candidate in (base / ".anvil", base / "bin" / ".anvil"):
            if candidate in seen:
                continue
            seen.add(candidate)
            if (candidate / "state.db").is_file():
                legacy_src = candidate
                break
        if legacy_src is not None:
            break

    if legacy_src is None:
        _report(json_output, {
            "status": "no_legacy_state",
            "source": None,
            "target": _display_path(target_anvil),
            "applied": False,
            "message": (
                f"No legacy in-repo .anvil/ with a state.db found under {_display_path(root)}; "
                f"nothing to migrate."
            ),
        })
        return

    # Pending recovery binds physical identities in the original State. Copying
    # it would publish a new default authority that cannot resume that recovery.
    try:
        _require_no_event_recovery(legacy_src / "state.db")
    except SchemaProbeFailed as exc:
        if json_output:
            fail(_COMMAND, str(exc), code="migration_failed")
        typer.echo(f"Error: migration failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    files = sum(1 for p in legacy_src.rglob("*") if p.is_file())

    if not yes:
        _report(json_output, {
            "status": "dry_run",
            "source": _display_path(legacy_src),
            "target": _display_path(target_anvil),
            "applied": False,
            "files": files,
            "message": (
                f"Would copy {files} file(s) from {_display_path(legacy_src)} → "
                f"{_display_path(target_anvil)}. "
                f"Dry run — nothing written. Re-run with --yes to apply."
            ),
        })
        return

    # Apply: copy into a temp sibling (in the SAME parent as the target, so the
    # rename is intra-filesystem — no EXDEV), then atomically rename into place.
    # The legacy source is never modified.
    staging = target_base / ".anvil.migrating"
    staging_owned = False
    try:
        database = str(legacy_src / "state.db")
        events = str(legacy_src / "events.jsonl")
        with ExitStack() as custody:
            # Preserve opaque legacy bytes: do not initialize State, create a
            # source token or manufacture a missing authority log.
            custody.enter_context(_state_operation_lock(
                database, contention_error_type=SchemaProbeFailed,
            ))
            if os.path.lexists(events):
                custody.enter_context(_schema_initialization_lock(
                    database, events, create_missing_log=False,
                ))
            _require_no_event_recovery(database)
            target_base.mkdir(parents=True, exist_ok=True)
            if staging.exists():
                shutil.rmtree(staging)
            staging_owned = True
            with BoundedFileInspection() as inspection:
                inspection.copy_tree(legacy_src, staging)
            _require_current_state_lock(database, contention_error_type=SchemaProbeFailed)
            _require_no_event_recovery(database)
            _require_no_event_recovery(staging / "state.db")
            os.replace(staging, target_anvil)  # atomic; target_anvil confirmed absent above
    except (OSError, SchemaProbeFailed) as exc:
        if staging_owned:
            shutil.rmtree(staging, ignore_errors=True)  # remove only this attempt's partial copy
        if json_output:
            fail(_COMMAND, str(exc), code="migration_failed")
        typer.echo(f"Error: migration failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    _report(json_output, {
        "status": "migrated",
        "source": _display_path(legacy_src),
        "target": _display_path(target_anvil),
        "applied": True,
        "files": files,
        "message": (
            f"Migrated {files} file(s) from {_display_path(legacy_src)} → "
            f"{_display_path(target_anvil)}. The legacy directory was left in "
            "place as a fallback."
        ),
    })


def _emit_report(json_output: bool, data: dict[str, Any]) -> None:
    if json_output:
        emit_success(_COMMAND, data)
    else:
        typer.echo(data["message"])


def _adopt_refused(json_output: bool, message: str, code: str) -> None:
    if json_output:
        fail(_COMMAND, message, code=code)
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


def _mtime_iso(path: Path) -> str | None:
    """A cheap "is this the history I expect" detail for the dry-run report."""
    try:
        entry = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(entry.st_mode):
        return None
    return datetime.datetime.fromtimestamp(entry.st_mtime, datetime.UTC).isoformat(
        timespec="seconds"
    )


def _adopt_legacy_workspace(
    loc: Path, root: Path, *, yes: bool, json_output: bool
) -> None:
    """Bind an unclaimed bare-name home workspace to ``root`` (B44-1).

    The only writer of the owner marker. Nothing is copied or moved: the
    workspace keeps its path and, once bound, resolves for this project only.
    """
    legacy = _legacy_workspace_base(root)
    legacy_anvil = legacy / _STATE_DIR_NAME
    hashed_anvil = (legacy.parent / _workspace_key(root)) / _STATE_DIR_NAME
    base: dict[str, Any] = {
        "legacy_workspace": _display_path(legacy),
        "canonical_root": _display_path(root),
        "applied": False,
    }

    if not (legacy_anvil / "state.db").exists():
        _emit_report(json_output, {
            **base,
            "status": "no_legacy_workspace",
            "message": (
                f"No legacy bare-name workspace with a state.db at "
                f"{_display_path(legacy)}; nothing to adopt."
            ),
        })
        return

    try:
        anvil_entry = legacy_anvil.lstat()
    except OSError as exc:
        _adopt_refused(json_output, f"cannot inspect {_display_path(legacy_anvil)}: {exc}",
                       "adoption_failed")
        return
    if not stat.S_ISDIR(anvil_entry.st_mode) or getattr(
        anvil_entry, "st_file_attributes", 0
    ) & 0x400:
        _adopt_refused(
            json_output,
            f"{_display_path(legacy_anvil)} is not a real directory; refusing to adopt it.",
            "adoption_failed",
        )

    owner_state, owner = read_workspace_owner(legacy_anvil)
    if owner_state == "valid" and owner is not None:
        if workspace_owner_matches(owner, root):
            _emit_report(json_output, {
                **base,
                "status": "already_adopted",
                "message": (
                    f"Legacy workspace {_display_path(legacy)} is already bound to "
                    f"{_display_path(root)}; nothing to do."
                ),
            })
            return
        _adopt_refused(
            json_output,
            f"legacy workspace {_display_path(legacy)} is bound to a different project "
            f"({owner}); it cannot be adopted by {_display_path(root)}. Run "
            "`anvil init` to start a separate workspace for this project.",
            "legacy_workspace_owned",
        )
    if owner_state == "invalid":
        _adopt_refused(
            json_output,
            f"{_display_path(legacy_anvil / WORKSPACE_OWNER_MARKER)} exists but is not a "
            "valid owner marker (a link, an oversized file, or malformed content). "
            "Inspect it and remove it before adopting this workspace.",
            "legacy_marker_invalid",
        )

    # No silent winner between two real workspaces: the operator decides.
    if hashed_anvil.exists():
        _adopt_refused(
            json_output,
            f"this project already has a workspace at {_display_path(hashed_anvil)}, and "
            f"an unclaimed legacy workspace exists at {_display_path(legacy_anvil)}. "
            "Anvil will not choose between them: keep the current one (leave the legacy "
            "directory alone or archive it), or move the current one aside and re-run "
            "this command to adopt the legacy workspace.",
            "workspace_conflict",
        )

    detail = {
        "events_modified_at": _mtime_iso(legacy_anvil / "events.jsonl"),
        "state_modified_at": _mtime_iso(legacy_anvil / "state.db"),
    }
    seen = detail["events_modified_at"] or detail["state_modified_at"] or "unknown"
    if not yes:
        _emit_report(json_output, {
            **base,
            **detail,
            "status": "adopt_dry_run",
            "message": (
                f"Would bind legacy workspace {_display_path(legacy)} (last written "
                f"{seen}) to project root {_display_path(root)}. A bare-name workspace "
                "is keyed on the directory name alone — confirm this history belongs "
                "to this project. Dry run — nothing written. Re-run with "
                "--adopt-legacy --yes to apply."
            ),
        })
        return

    try:
        write_workspace_owner(legacy_anvil, root)
    except OSError as exc:
        _adopt_refused(json_output, f"adoption failed: {exc}", "adoption_failed")
    _emit_report(json_output, {
        **base,
        **detail,
        "status": "adopted",
        "applied": True,
        "message": (
            f"Bound legacy workspace {_display_path(legacy)} to project root "
            f"{_display_path(root)}. It now resolves for this project only."
        ),
    })
