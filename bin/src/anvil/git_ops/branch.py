"""Git branch helpers for anvil claim flow.

Pure subprocess wrappers — no git Python library dependency.  The install
footprint stays small (Phase 1 policy: no heavy deps for optional features).

All public functions return dataclasses rather than raising on git failures.
The CLI translates a created=False result into a one-line stderr warning and
continues; anvil must work without git.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from anvil.naming import safe_path_component

# Hard ceilings to keep git ops bounded even on misbehaving systems.
# Critic flagged that the original code had no subprocess timeout (a hung git
# binary would freeze the claim flow) and no collision-loop ceiling (an
# unbounded while-exists loop on a corrupted repo).
_GIT_TIMEOUT_SECONDS = 10
_MAX_COLLISION_ATTEMPTS = 20


@dataclass(frozen=True)
class BranchResult:
    """Result of a create_branch_for_task() call."""

    branch: str | None     # actual branch name created; None when created=False
    created: bool          # True iff the branch was created in this call
    reason: str | None     # why created=False, OR a warning string when created=True


def _slug(text: str) -> str:
    """Lowercase, alphanumeric + hyphens, max 40 chars.

    Example: "My Feature: Auth Tokens!" → "my-feature--auth-tokens-"
             collapsed → "my-feature-auth-tokens"
    """
    lowered = text.lower()
    replaced = re.sub(r"[^a-z0-9]+", "-", lowered)
    collapsed = re.sub(r"-{2,}", "-", replaced)
    stripped = collapsed.strip("-")
    return (stripped or "task")[:40]


def is_git_available() -> bool:
    """True if the `git` binary is on PATH."""
    return shutil.which("git") is not None


def is_git_repo(cwd: Path) -> bool:
    """True if *cwd* is inside a git repository.

    Uses `git rev-parse --git-dir` which exits 0 inside any repo and non-zero
    outside.  Stderr is suppressed so we never emit git noise to the user.
    A timeout treats a hung git binary as "not a repo" — same end result.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(cwd),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _branch_exists(branch: str, cwd: Path) -> bool:
    """Return True if *branch* already exists locally."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=str(cwd),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # Treat timeout as "exists" — safer than "doesn't exist" which would
        # try to create a duplicate and likely fail again.
        return True
    return result.returncode == 0


def create_branch_for_task(
    task_id: str,
    title: str,
    *,
    cwd: Path,
    base: str | None = None,
    branch_prefix: str = "agent",
    checkout: bool = True,
) -> BranchResult:
    """Create a ``<branch_prefix>/<task_id_lower>-<slug>`` branch in *cwd*.

    Behavior:
    - If git not available OR not a git repo → BranchResult(None, False, reason)
      (the CLI warns but does NOT fail the claim).
    - Builds: ``<branch_prefix>/<sanitized_task_id.lower()>-<slug(title)>``
      truncated to 80 chars, where the task id is run through
      :func:`anvil.naming.safe_path_component` so a namespaced id (``prd:T001``)
      yields a valid refname instead of one with an illegal ``:`` (#108.1). When
      ``branch_prefix`` is empty, the leading prefix + separator is omitted.
    - If that branch already exists, appends -2, -3, … until a unique name is found.
    - When ``checkout`` (default), runs ``git checkout -b <branch>`` — moving the
      current worktree onto the branch. When ``checkout=False``, runs
      ``git branch <branch>`` — creating the ref WITHOUT checking it out, so a
      caller (e.g. ``claim --worktree``) can hand the branch to ``git worktree
      add``; a branch already checked out in the main worktree can't be added to
      another one.
    - On success: BranchResult(branch, True, None).
    - On collision-rename: BranchResult(branch_with_suffix, True, "renamed due to collision").
    - On git error: BranchResult(None, False, str(error)).

    Args:
        task_id:       The task identifier (e.g. "T001"). Lowercased before use.
        title:         Human-readable task title, converted to a slug.
        cwd:           Directory in which to run git commands.
        base:          Optional base ref to branch off. If None, branches off HEAD.
        branch_prefix: Prefix to prepend (default ``"agent"``). v1.15.0+
                       reads this from ``config.yaml`` so host projects with
                       ``feature/`` or ``fix/`` conventions get matching
                       branches instead of the silently-incompatible default.
                       Empty string opts out of any prefix.

    Returns:
        BranchResult describing what happened (or why it was skipped).
    """
    if not is_git_available():
        return BranchResult(None, False, "git not available on PATH")

    if not is_git_repo(cwd):
        return BranchResult(None, False, "not a git repository")

    safe_id = safe_path_component(task_id).lower()
    if branch_prefix:
        base_name = f"{branch_prefix}/{safe_id}-{_slug(title)}"
    else:
        base_name = f"{safe_id}-{_slug(title)}"
    # Truncate to 80 chars total to stay well under git's 250-byte limit
    # while keeping branch names scannable.
    base_name = base_name[:80]

    # Resolve a unique branch name (handle collisions with -2, -3, …).
    # Capped at _MAX_COLLISION_ATTEMPTS to prevent an unbounded loop on a
    # corrupted repo (critic flagged this).
    branch = base_name
    collision_suffix = 2
    renamed = False
    while _branch_exists(branch, cwd):
        if collision_suffix > _MAX_COLLISION_ATTEMPTS + 1:
            return BranchResult(
                None,
                False,
                (
                    f"too many branch collisions for {base_name!r} "
                    f"(tried up to suffix -{_MAX_COLLISION_ATTEMPTS})"
                ),
            )
        branch = f"{base_name}-{collision_suffix}"[:80]
        collision_suffix += 1
        renamed = True

    # Build the git command: check out onto the branch (checkout -b) or just
    # create the ref without moving the current worktree (branch).
    verb = ["checkout", "-b"] if checkout else ["branch"]
    cmd = ["git", *verb, branch]
    if base is not None:
        cmd.append(base)

    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return BranchResult(
            None, False, f"git {' '.join(verb)} timed out after {_GIT_TIMEOUT_SECONDS}s"
        )
    if result.returncode != 0:
        error_msg = (result.stderr or result.stdout or "unknown git error").strip()
        return BranchResult(None, False, error_msg)

    warning = "renamed due to collision" if renamed else None
    return BranchResult(branch, True, warning)


def use_named_branch(
    name: str,
    *,
    cwd: Path,
    base: str | None = None,
) -> BranchResult:
    """Attach a claim to a caller-supplied / existing branch *name* (T027).

    Unlike :func:`create_branch_for_task`, this does NOT generate an
    ``agent/<task>-<slug>`` name — it meets the user where their git workflow
    already is by checking out a branch they name directly.

    Behavior:
    - If git not available OR not a git repo → BranchResult(None, False, reason)
      (the CLI warns but does NOT fail the claim, same contract as
      create_branch_for_task).
    - If a local branch *name* already exists → ``git checkout <name>`` and
      return BranchResult(name, True, "checked out existing branch").
    - Otherwise → ``git checkout -b <name>`` (optionally off *base*) and return
      BranchResult(name, True, None).
    - On git error → BranchResult(None, False, str(error)).

    The branch name is used verbatim (no slugging), so the caller controls the
    exact ref. Git itself rejects invalid ref names, which surfaces as a
    created=False warning.

    Args:
        name: Exact branch name to attach to (existing or to-be-created).
        cwd:  Directory in which to run git commands.
        base: Optional base ref to branch off when creating a NEW branch.
              Ignored when the branch already exists.

    Returns:
        BranchResult describing what happened (or why it was skipped).
    """
    if not is_git_available():
        return BranchResult(None, False, "git not available on PATH")

    if not is_git_repo(cwd):
        return BranchResult(None, False, "not a git repository")

    already_exists = _branch_exists(name, cwd)

    if already_exists:
        cmd = ["git", "checkout", name]
    else:
        cmd = ["git", "checkout", "-b", name]
        if base is not None:
            cmd.append(base)

    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return BranchResult(
            None, False, f"git checkout timed out after {_GIT_TIMEOUT_SECONDS}s"
        )
    if result.returncode != 0:
        error_msg = (result.stderr or result.stdout or "unknown git error").strip()
        return BranchResult(None, False, error_msg)

    reason = "checked out existing branch" if already_exists else None
    return BranchResult(name, True, reason)
