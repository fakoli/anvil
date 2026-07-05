"""Git worktree helpers for anvil claim flow.

Pure subprocess wrappers — no git Python library dependency.

All public functions return dataclasses rather than raising on git failures.
The CLI translates a created=False result into a one-line stderr warning.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from anvil.git_ops.branch import _GIT_TIMEOUT_SECONDS, is_git_available, is_git_repo
from anvil.naming import safe_path_component


@dataclass(frozen=True)
class WorktreeResult:
    """Result of a create_worktree_for_task() call."""

    path: str | None    # absolute path of the worktree; None when created=False
    created: bool       # True iff the worktree was created in this call
    reason: str | None  # why created=False; None on success


def _is_dirty(cwd: Path) -> bool:
    """Return True if the working tree has uncommitted changes.

    Uses ``git status --porcelain`` — any output means dirty. A timeout
    is treated as dirty (safer: refuse to add a worktree on top of a
    possibly-modified tree).
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return True
    return bool(result.stdout.strip())


def create_worktree_for_task(
    task_id: str,
    branch: str,
    *,
    cwd: Path,
    parent_dir: Path | None = None,
) -> WorktreeResult:
    """Create a git worktree for *branch* adjacent to *cwd*.

    Behavior:
    - If git not available OR not a git repo → WorktreeResult(None, False, reason).
    - If the working tree is dirty → WorktreeResult(None, False, "dirty worktree ...").
    - Worktree path: parent_dir if supplied, else cwd.parent / f"wt-{task_id.lower()}".
    - Runs ``git worktree add <path> <branch>``.
    - On success: WorktreeResult(str(path), True, None).
    - On failure: WorktreeResult(None, False, str(error)).

    Args:
        task_id:    The task identifier used to name the worktree directory.
        branch:     The git branch that the new worktree should check out.
        cwd:        Directory in which to run git commands (the main repo root).
        parent_dir: Override for the worktree directory path.

    Returns:
        WorktreeResult describing what happened (or why it was skipped).
    """
    if not is_git_available():
        return WorktreeResult(None, False, "git not available on PATH")

    if not is_git_repo(cwd):
        return WorktreeResult(None, False, "not a git repository")

    if _is_dirty(cwd):
        return WorktreeResult(
            None, False, "dirty worktree — commit or stash changes before adding a worktree"
        )

    wt_path = (
        parent_dir if parent_dir is not None
        else cwd.parent / f"wt-{safe_path_component(task_id).lower()}"
    )

    try:
        result = subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return WorktreeResult(
            None, False, f"git worktree add timed out after {_GIT_TIMEOUT_SECONDS}s"
        )
    if result.returncode != 0:
        error_msg = (result.stderr or result.stdout or "unknown git error").strip()
        return WorktreeResult(None, False, error_msg)

    return WorktreeResult(str(wt_path), True, None)
