"""Base-freshness and merged-tree conflict probes (retro-opps:T005).

The merge-safety seam: answers "is this branch based on the current
integration branch, and would merging it conflict?" WITHOUT touching the
user's working tree. Pure subprocess wrappers in the worktree.py style —
every failure path returns a report dataclass, never raises, and every git
call is timeout-bounded.

Local-first degradation is structural: no remote named ``origin``, or a
fetch that fails/times out, degrades to the LOCAL default branch with
``remote_checked=False`` and a reason string — never a hard failure. An
offline project always gets an answer.

The textual-conflict probe uses ``git merge-tree --write-tree`` (git >= 2.38),
which writes tree OBJECTS into the object database but never modifies the
working tree or index. On older git the probe reports itself skipped, not
failed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from anvil.git_ops.branch import (
    _GIT_TIMEOUT_SECONDS,
    is_git_available,
    is_git_repo,
)

__all__ = [
    "BaseRef",
    "FreshnessReport",
    "check_freshness",
    "resolve_base",
]


@dataclass(frozen=True)
class BaseRef:
    """The integration base a branch is measured against."""

    ref: str | None      # e.g. "origin/main" or "main"; None when unresolvable
    remote_checked: bool  # True iff origin exists AND the fetch succeeded
    reason: str | None    # why remote_checked=False or ref=None; None otherwise


@dataclass(frozen=True)
class FreshnessReport:
    """Result of a check_freshness() call. Never raised, always returned."""

    base: BaseRef
    behind_count: int | None   # commits on base missing from branch; None = probe failed
    has_conflicts: bool | None  # None = conflict probe unavailable/failed
    conflict_probe: str         # "merge-tree" or "skipped: <reason>"
    reason: str | None          # why a probe failed; None when everything ran

    @property
    def is_stale(self) -> bool:
        """True when the branch is VERIFIABLY behind its base.

        Tri-state caution: ``behind_count is None`` (probe failed) also
        returns False — "not verifiably stale" is not "fresh". A strict
        gate must check ``behind_count == 0`` explicitly rather than
        ``not report.is_stale``, or it silently fails open on probe errors.
        """
        return bool(self.behind_count)


def _run_git(
    args: list[str], cwd: Path
) -> subprocess.CompletedProcess[str] | None:
    """Run a git command; None on timeout or missing binary (never raises)."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _ref_exists(ref: str, cwd: Path) -> bool:
    result = _run_git(["rev-parse", "--verify", "--quiet", ref], cwd)
    return result is not None and result.returncode == 0


def _local_default_branch(cwd: Path) -> str | None:
    """Best-effort local default branch: init.defaultBranch conventions."""
    for candidate in ("main", "master"):
        if _ref_exists(candidate, cwd):
            return candidate
    return None


def resolve_base(cwd: Path) -> BaseRef:
    """Resolve the integration base for the repo at *cwd*.

    Preference order:

    1. ``origin/<default>`` when a remote named ``origin`` exists and a
       timeout-bounded ``git fetch origin`` succeeds (``remote_checked=True``).
       The default branch comes from ``refs/remotes/origin/HEAD``, falling
       back to ``origin/main`` / ``origin/master`` existence.
    2. The LOCAL default branch (``main``/``master``) with
       ``remote_checked=False`` and a reason — the offline/local-first path.
    3. ``BaseRef(None, False, reason)`` when nothing resolves (not a repo,
       git missing, or no recognizable default branch). Never raises.
    """
    if not cwd.is_dir():
        return BaseRef(
            ref=None, remote_checked=False, reason="directory does not exist"
        )
    if not is_git_available():
        return BaseRef(ref=None, remote_checked=False, reason="git not available")
    if not is_git_repo(cwd):
        return BaseRef(ref=None, remote_checked=False, reason="not a git repository")

    remote = _run_git(["remote", "get-url", "origin"], cwd)
    if remote is None or remote.returncode != 0:
        local = _local_default_branch(cwd)
        if local is None:
            return BaseRef(
                ref=None,
                remote_checked=False,
                reason="no remote 'origin' and no local main/master branch",
            )
        return BaseRef(
            ref=local, remote_checked=False, reason="no remote named 'origin'"
        )

    fetch = _run_git(["fetch", "origin", "--quiet"], cwd)
    if fetch is None or fetch.returncode != 0:
        detail = (
            "timed out"
            if fetch is None
            else (fetch.stderr.strip() or f"exit {fetch.returncode}")
        )
        local = _local_default_branch(cwd)
        if local is None:
            return BaseRef(
                ref=None,
                remote_checked=False,
                reason=f"fetch failed ({detail}) and no local main/master branch",
            )
        return BaseRef(
            ref=local, remote_checked=False, reason=f"fetch failed ({detail})"
        )

    head = _run_git(
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd
    )
    if head is not None and head.returncode == 0 and head.stdout.strip():
        return BaseRef(ref=head.stdout.strip(), remote_checked=True, reason=None)
    for candidate in ("origin/main", "origin/master"):
        if _ref_exists(candidate, cwd):
            return BaseRef(ref=candidate, remote_checked=True, reason=None)

    # Remote reachable but its default branch is unrecognizable; degrade local.
    local = _local_default_branch(cwd)
    if local is None:
        return BaseRef(
            ref=None,
            remote_checked=True,
            reason="origin fetched but no origin/main|master or local default found",
        )
    return BaseRef(
        ref=local,
        remote_checked=True,
        reason="origin fetched but its default branch was unrecognizable",
    )


def _conflict_probe(
    base_ref: str, branch: str, cwd: Path
) -> tuple[bool | None, str]:
    """Textual-conflict probe via ``git merge-tree --write-tree``.

    Returns ``(has_conflicts, probe_label)``. Writes tree objects to the
    object db only — the working tree and index are never touched. Exit 0 =
    clean merge, exit 1 = conflicts; anything else (including an "unknown
    option" error on git < 2.38) reports the probe as skipped, not failed.
    """
    result = _run_git(
        ["merge-tree", "--write-tree", "--name-only", base_ref, branch], cwd
    )
    if result is None:
        return None, "skipped: merge-tree timed out or git missing"
    if result.returncode == 0:
        return False, "merge-tree"
    if result.returncode == 1:
        return True, "merge-tree"
    detail = result.stderr.strip().splitlines()[0] if result.stderr.strip() else (
        f"exit {result.returncode}"
    )
    return None, f"skipped: {detail}"


def check_freshness(
    branch: str, *, cwd: Path, base: BaseRef | None = None
) -> FreshnessReport:
    """Measure *branch* against its integration base.

    ``behind_count`` is the number of commits reachable from the base but not
    from the branch (``git rev-list --count <branch>..<base>``) — 0 means the
    branch contains everything on the base. ``has_conflicts`` is the
    merge-tree textual probe (None when unavailable). Every failure path
    returns a report with a reason; nothing raises, nothing writes to the
    working tree.
    """
    resolved = base if base is not None else resolve_base(cwd)
    if resolved.ref is None:
        return FreshnessReport(
            base=resolved,
            behind_count=None,
            has_conflicts=None,
            conflict_probe="skipped: no base ref",
            reason=resolved.reason,
        )
    # Re-validate caller-supplied base refs: merge-tree exits 1 for BOTH a
    # real conflict and a nonexistent ref, so a stale resolve-once/reuse
    # BaseRef would otherwise read as a false has_conflicts=True.
    if base is not None and not _ref_exists(resolved.ref, cwd):
        return FreshnessReport(
            base=resolved,
            behind_count=None,
            has_conflicts=None,
            conflict_probe="skipped: base ref missing",
            reason=f"base ref '{resolved.ref}' not found",
        )
    if not _ref_exists(branch, cwd):
        return FreshnessReport(
            base=resolved,
            behind_count=None,
            has_conflicts=None,
            conflict_probe="skipped: branch missing",
            reason=f"branch '{branch}' not found",
        )

    count = _run_git(["rev-list", "--count", f"{branch}..{resolved.ref}"], cwd)
    behind: int | None
    reason: str | None = None
    if count is None or count.returncode != 0:
        behind = None
        reason = "rev-list failed" if count is None else (
            count.stderr.strip() or f"rev-list exit {count.returncode}"
        )
    else:
        try:
            behind = int(count.stdout.strip())
        except ValueError:
            behind = None
            reason = f"unparseable rev-list output: {count.stdout.strip()!r}"

    has_conflicts, probe = _conflict_probe(resolved.ref, branch, cwd)
    return FreshnessReport(
        base=resolved,
        behind_count=behind,
        has_conflicts=has_conflicts,
        conflict_probe=probe,
        reason=reason,
    )
