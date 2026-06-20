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
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import typer

from anvil.cli._helpers import _canonical_project_root, _home_workspace_base
from anvil.cli._json import JSON_OPTION, emit_success, fail

__all__ = ["migrate_workspace"]

_COMMAND = "migrate-workspace"


def migrate_workspace(
    yes: bool = typer.Option(  # noqa: B008
        False,
        "--yes",
        help="Apply the migration. Without this it is a dry run (reports, writes nothing).",
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
    """
    loc = cwd or Path.cwd()
    root = _canonical_project_root(loc)
    target_base = _home_workspace_base(loc)
    target_anvil = target_base / ".anvil"

    # No-clobber: an existing home workspace is authoritative — skip entirely.
    if target_anvil.exists():
        _report(json_output, {
            "status": "already_migrated",
            "source": None,
            "target": str(target_anvil),
            "applied": False,
            "message": (
                f"Home workspace already exists at {target_anvil}; nothing to migrate "
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
            "target": str(target_anvil),
            "applied": False,
            "message": (
                f"No legacy in-repo .anvil/ with a state.db found under {root}; "
                f"nothing to migrate."
            ),
        })
        return

    files = sum(1 for p in legacy_src.rglob("*") if p.is_file())

    if not yes:
        _report(json_output, {
            "status": "dry_run",
            "source": str(legacy_src),
            "target": str(target_anvil),
            "applied": False,
            "files": files,
            "message": (
                f"Would copy {files} file(s) from {legacy_src} → {target_anvil}. "
                f"Dry run — nothing written. Re-run with --yes to apply."
            ),
        })
        return

    # Apply: copy into a temp sibling (in the SAME parent as the target, so the
    # rename is intra-filesystem — no EXDEV), then atomically rename into place.
    # The legacy source is never modified.
    staging = target_base / ".anvil.migrating"
    try:
        target_base.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(legacy_src, staging)  # dirs_exist_ok=False — staging is fresh
        os.replace(staging, target_anvil)  # atomic; target_anvil confirmed absent above
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)  # never leave a partial staging dir
        if json_output:
            fail(_COMMAND, str(exc), code="migration_failed")
        typer.echo(f"Error: migration failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    _report(json_output, {
        "status": "migrated",
        "source": str(legacy_src),
        "target": str(target_anvil),
        "applied": True,
        "files": files,
        "message": (
            f"Migrated {files} file(s) from {legacy_src} → {target_anvil}. The legacy "
            f"directory was left in place as a fallback."
        ),
    })


def _report(json_output: bool, data: dict[str, Any]) -> None:
    if json_output:
        emit_success(_COMMAND, data)
    else:
        typer.echo(data["message"])
