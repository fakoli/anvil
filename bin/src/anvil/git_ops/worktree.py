"""Git worktree helpers for anvil claim flow.

Pure subprocess wrappers — no git Python library dependency.

All public functions return dataclasses rather than raising on git failures.
The CLI translates a created=False result into a one-line stderr warning.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from anvil.git_ops.branch import (
    _GIT_TIMEOUT_SECONDS,
    _MAX_COLLISION_ATTEMPTS,
    _branch_name_candidate,
    branch_name_for_task,
    is_git_available,
    is_git_repo,
)
from anvil.naming import safe_path_component

__all__ = [
    "ClaimGitPlan",
    "ClaimPlanError",
    "ClaimPlanPrecondition",
    "GitWorktree",
    "WorktreeResult",
    "canonical_git_root",
    "create_worktree_for_task",
    "resolve_claim_plan",
    "tree_state",
]


@dataclass(frozen=True)
class WorktreeResult:
    """Result of a create_worktree_for_task() call."""

    path: str | None    # absolute path of the worktree; None when created=False
    created: bool       # True iff the worktree was created in this call
    reason: str | None  # why created=False; None on success


class ClaimPlanError(RuntimeError):
    """A bounded refusal produced before claim or Git mutation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GitWorktree:
    """One entry from ``git worktree list --porcelain``."""

    path: str
    head_sha: str
    branch_ref: str | None
    detached: bool
    bare: bool
    locked_reason: str | None
    prunable_reason: str | None


@dataclass(frozen=True)
class ClaimPlanPrecondition:
    """One exact observation T012 must revalidate immediately before mutation."""

    kind: Literal["ref_oid", "caller_head", "caller_clean", "topology", "path"]
    subject: str
    expected: str


@dataclass(frozen=True)
class ClaimGitPlan:
    """Read-only, deterministic Git intent for one task claim."""

    mode: Literal["isolated", "shared", "state_only"]
    git_metadata_available: bool
    caller_path: str
    caller_worktree_path: str | None
    canonical_root: str | None
    linked_worktree: bool | None
    caller_dirty: bool | None
    caller_head_ref: str | None
    caller_head_sha: str | None
    default_discovery: str | None
    local_default_ref: str | None
    local_default_sha: str | None
    upstream_default_ref: str | None
    upstream_default_sha: str | None
    selected_default_base_ref: str | None
    selected_default_base_sha: str | None
    claim_start_ref: str | None
    claim_start_sha: str | None
    branch: str | None
    branch_exists: bool
    branch_owner_path: str | None
    target_path: str | None
    target_owner_branch: str | None
    worktrees: tuple[GitWorktree, ...]
    warnings: tuple[str, ...]
    revalidation_preconditions: tuple[ClaimPlanPrecondition, ...]


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=env,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None


def _git_value(args: list[str], cwd: Path, *, code: str) -> str:
    result = _run_git(args, cwd)
    if result is None:
        raise ClaimPlanError(code, "Git observation timed out or is unavailable")
    if result.returncode != 0:
        raise ClaimPlanError(code, "Git observation failed")
    return result.stdout.strip()


def _path_key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def canonical_git_root(cwd: Path) -> Path | None:
    """Return the main worktree root shared by linked/nested callers."""
    if shutil.which("git") is None:
        return None
    result = _run_git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd,
    )
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve(strict=False).parent


def _ref_oid(ref: str, cwd: Path) -> str | None:
    present = _run_git(["rev-parse", "--verify", "--quiet", ref], cwd)
    if present is None:
        raise ClaimPlanError("git_observation_failed", "Git ref observation failed")
    if present.returncode == 1:
        return None
    if present.returncode != 0:
        raise ClaimPlanError("git_observation_failed", "Git ref observation failed")
    result = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd)
    if result is None or result.returncode != 0:
        raise ClaimPlanError(
            "invalid_ref_target",
            "A required Git ref does not identify a commit",
        )
    oid = result.stdout.strip()
    if len(oid) != 40:
        raise ClaimPlanError("git_observation_failed", "Git returned an invalid ref OID")
    return oid


def _is_ancestor(ancestor: str, descendant: str, cwd: Path) -> bool | None:
    result = _run_git(["merge-base", "--is-ancestor", ancestor, descendant], cwd)
    if result is None:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _worktree_topology(cwd: Path) -> tuple[GitWorktree, ...]:
    raw = _git_value(
        ["worktree", "list", "--porcelain", "-z"],
        cwd,
        code="worktree_topology_unavailable",
    )
    records: list[GitWorktree] = []
    current: dict[str, str | bool] = {}

    def finish() -> None:
        if not current:
            return
        path = current.get("worktree")
        head = current.get("HEAD")
        if not isinstance(path, str) or not isinstance(head, str):
            raise ClaimPlanError(
                "worktree_topology_invalid",
                "Git worktree topology is incomplete",
            )
        branch_value = current.get("branch")
        records.append(
            GitWorktree(
                path=str(Path(path).resolve(strict=False)),
                head_sha=head,
                branch_ref=branch_value if isinstance(branch_value, str) else None,
                detached=bool(current.get("detached", False)),
                bare=bool(current.get("bare", False)),
                locked_reason=(
                    str(current["locked"]) if "locked" in current else None
                ),
                prunable_reason=(
                    str(current["prunable"]) if "prunable" in current else None
                ),
            )
        )
        current.clear()

    for field in raw.split("\0"):
        if not field:
            finish()
            continue
        key, _, value = field.partition(" ")
        if key in {"detached", "bare"}:
            current[key] = True
        elif key in {"worktree", "HEAD", "branch", "locked", "prunable"}:
            current[key] = value
    finish()
    if not records:
        raise ClaimPlanError("worktree_topology_invalid", "Git reported no worktrees")
    return tuple(sorted(records, key=lambda item: _path_key(item.path)))


def _topology_digest(worktrees: tuple[GitWorktree, ...]) -> str:
    material = [
        {
            "path": item.path,
            "head_sha": item.head_sha,
            "branch_ref": item.branch_ref,
            "detached": item.detached,
            "bare": item.bare,
            "locked_reason": item.locked_reason,
            "prunable_reason": item.prunable_reason,
        }
        for item in worktrees
    ]
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _discover_local_default(
    cwd: Path,
    *,
    caller_head_ref: str | None,
) -> tuple[str, str]:
    remote_head = _run_git(
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd,
    )
    candidates: list[tuple[str, str]] = []
    if remote_head is not None and remote_head.returncode == 0:
        short = remote_head.stdout.strip()
        if "/" in short:
            candidates.append(
                (f"refs/heads/{short.split('/', 1)[1]}", "remote HEAD name")
            )
    candidates.extend(
        [
            ("refs/heads/main", "local main"),
            ("refs/heads/master", "local master"),
        ]
    )
    if caller_head_ref is not None:
        candidates.append((caller_head_ref, "caller HEAD fallback"))
    seen: set[str] = set()
    for ref, source in candidates:
        if ref in seen:
            continue
        seen.add(ref)
        if _ref_oid(ref, cwd) is not None:
            return ref, source
    raise ClaimPlanError(
        "default_ref_unavailable",
        "No local default branch can be resolved without fetching",
    )


def _configured_upstream(local_ref: str, cwd: Path) -> str | None:
    result = _run_git(
        ["for-each-ref", "--format=%(upstream)", local_ref],
        cwd,
    )
    if result is None:
        raise ClaimPlanError(
            "git_observation_failed",
            "Configured upstream observation failed",
        )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _select_default_base(
    local_ref: str,
    upstream_ref: str | None,
    cwd: Path,
) -> tuple[str, str, str, str | None, tuple[str, ...]]:
    local_oid = _ref_oid(local_ref, cwd)
    if local_oid is None:
        raise ClaimPlanError("default_ref_unavailable", "Local default ref disappeared")
    warnings: list[str] = []
    if upstream_ref is None:
        warnings.append(
            "local default has no configured locally known upstream; "
            "using it without a network freshness check"
        )
        return local_ref, local_oid, local_oid, None, tuple(warnings)
    upstream_oid = _ref_oid(upstream_ref, cwd)
    if upstream_oid is None:
        warnings.append(
            "configured upstream ref is unavailable locally; using the local default"
        )
        return local_ref, local_oid, local_oid, None, tuple(warnings)
    warnings.append(
        "upstream freshness was not fetched; planning used locally known refs only"
    )
    if local_oid == upstream_oid:
        return local_ref, local_oid, local_oid, upstream_oid, tuple(warnings)
    local_contains_upstream = _is_ancestor(upstream_oid, local_oid, cwd)
    upstream_contains_local = _is_ancestor(local_oid, upstream_oid, cwd)
    if local_contains_upstream is None or upstream_contains_local is None:
        warnings.append(
            "upstream ancestry could not be verified; using the local default"
        )
        return local_ref, local_oid, local_oid, upstream_oid, tuple(warnings)
    if local_contains_upstream:
        return local_ref, local_oid, local_oid, upstream_oid, tuple(warnings)
    if upstream_contains_local:
        return upstream_ref, upstream_oid, local_oid, upstream_oid, tuple(warnings)
    raise ClaimPlanError(
        "default_refs_diverged",
        "Local default and configured upstream have diverged",
    )


def resolve_claim_plan(
    task_id: str,
    title: str,
    *,
    cwd: Path,
    branch: str | None = None,
    branch_prefix: str = "agent",
    worktree: bool = False,
    isolation_required: bool = False,
    shared_tree: bool = False,
    target_path: Path | None = None,
) -> ClaimGitPlan:
    """Resolve a complete claim Git plan without fetching or mutating anything."""
    caller_path = cwd.resolve(strict=False)
    if not caller_path.is_dir():
        raise ClaimPlanError(
            "caller_path_unavailable",
            "Claim planning requires an existing caller directory",
        )
    isolated = worktree or isolation_required
    mode: Literal["isolated", "shared", "state_only"] = (
        "isolated" if isolated else "shared"
    )
    warnings: list[str] = []

    if shutil.which("git") is None or not is_git_repo(caller_path):
        if isolated:
            raise ClaimPlanError(
                "git_required",
                "An isolated claim requires an available Git worktree",
            )
        warnings.append(
            "Git metadata is unavailable; continuing with a state-only claim"
        )
        return ClaimGitPlan(
            mode="state_only",
            git_metadata_available=False,
            caller_path=str(caller_path),
            caller_worktree_path=None,
            canonical_root=None,
            linked_worktree=None,
            caller_dirty=None,
            caller_head_ref=None,
            caller_head_sha=None,
            default_discovery=None,
            local_default_ref=None,
            local_default_sha=None,
            upstream_default_ref=None,
            upstream_default_sha=None,
            selected_default_base_ref=None,
            selected_default_base_sha=None,
            claim_start_ref=None,
            claim_start_sha=None,
            branch=None,
            branch_exists=False,
            branch_owner_path=None,
            target_path=None,
            target_owner_branch=None,
            worktrees=(),
            warnings=tuple(warnings),
            revalidation_preconditions=(),
        )

    caller_root = Path(
        _git_value(
            ["rev-parse", "--show-toplevel"],
            caller_path,
            code="caller_topology_unavailable",
        )
    ).resolve(strict=False)
    canonical_root = canonical_git_root(caller_path)
    if canonical_root is None:
        raise ClaimPlanError(
            "caller_topology_unavailable",
            "Canonical Git root cannot be resolved",
        )
    linked = _path_key(caller_root) != _path_key(canonical_root)
    caller_head_sha = _git_value(
        ["rev-parse", "--verify", "HEAD^{commit}"],
        caller_path,
        code="caller_head_unavailable",
    )
    head_ref_result = _run_git(
        ["symbolic-ref", "--quiet", "HEAD"],
        caller_path,
    )
    caller_head_ref = (
        head_ref_result.stdout.strip()
        if head_ref_result is not None and head_ref_result.returncode == 0
        else None
    )
    dirty_result = _run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        caller_path,
    )
    if dirty_result is None or dirty_result.returncode != 0:
        raise ClaimPlanError("caller_dirty_unavailable", "Caller dirtiness is unknown")
    caller_dirty = bool(dirty_result.stdout)
    if not isolated and caller_dirty:
        raise ClaimPlanError(
            "dirty_shared_tree",
            "A shared-tree claim requires a clean caller worktree",
        )
    if not isolated and linked and not shared_tree:
        raise ClaimPlanError(
            "linked_shared_tree_not_authorized",
            "A linked caller requires explicit --shared-tree authorization",
        )
    if isolated and caller_dirty:
        warnings.append(
            "dirty caller is permitted because isolated planning never moves it"
        )

    topology = _worktree_topology(caller_path)
    caller_record = next(
        (item for item in topology if _path_key(item.path) == _path_key(caller_root)),
        None,
    )
    if caller_record is None:
        raise ClaimPlanError(
            "worktree_topology_invalid",
            "Caller worktree is absent from Git topology",
        )

    local_ref, discovery = _discover_local_default(
        caller_path,
        caller_head_ref=caller_head_ref,
    )
    upstream_ref = _configured_upstream(local_ref, caller_path)
    (
        selected_ref,
        selected_sha,
        local_sha,
        upstream_sha,
        base_warnings,
    ) = _select_default_base(local_ref, upstream_ref, caller_path)
    warnings.extend(base_warnings)

    planned_branch = branch
    branch_exists = False
    if planned_branch is not None:
        check_name = _run_git(["check-ref-format", "--branch", planned_branch], caller_path)
        if check_name is None or check_name.returncode != 0:
            raise ClaimPlanError("invalid_branch", "Named branch is invalid")
        branch_ref = f"refs/heads/{planned_branch}"
        branch_sha = _ref_oid(branch_ref, caller_path)
        branch_exists = branch_sha is not None
    else:
        base_name = branch_name_for_task(
            task_id,
            title,
            branch_prefix=branch_prefix,
        )
        for collision in range(_MAX_COLLISION_ATTEMPTS + 1):
            planned_branch = _branch_name_candidate(base_name, collision)
            if _ref_oid(f"refs/heads/{planned_branch}", caller_path) is None:
                break
        else:
            raise ClaimPlanError("branch_collision", "No unique branch name is available")
        branch_ref = f"refs/heads/{planned_branch}"
        branch_sha = None

    check_name = _run_git(["check-ref-format", "--branch", planned_branch], caller_path)
    if check_name is None or check_name.returncode != 0:
        raise ClaimPlanError("invalid_branch", "Planned branch is invalid")

    branch_owner = next(
        (item for item in topology if item.branch_ref == branch_ref),
        None,
    )
    if isolated and target_path is not None:
        target_candidate = (
            target_path
            if target_path.is_absolute()
            else canonical_root.parent / target_path
        )
        canonical_target = target_candidate.resolve(strict=False)
    else:
        canonical_target = None if isolated else caller_root
    if isolated and canonical_target is None:
        canonical_target = (
            canonical_root.parent
            / f"wt-{safe_path_component(task_id).lower()}"
        ).resolve(strict=False)

    if branch_exists:
        if branch_sha is None:  # Defensive narrowing; branch_exists implies this.
            raise ClaimPlanError("branch_disappeared", "Named branch disappeared")
        contains_base = _is_ancestor(selected_sha, branch_sha, caller_path)
        if contains_base is None:
            raise ClaimPlanError(
                "branch_ancestry_unavailable",
                "Named branch ancestry cannot be verified",
            )
        if not contains_base:
            raise ClaimPlanError(
                "branch_stale_or_diverged",
                "Named branch does not contain the selected default base",
            )
        claim_start_ref = branch_ref
        claim_start_sha = branch_sha
    else:
        claim_start_ref = selected_ref
        claim_start_sha = selected_sha

    if branch_owner is not None:
        compatible_owner = (
            _path_key(branch_owner.path) == _path_key(canonical_target)
        )
        if not compatible_owner:
            raise ClaimPlanError(
                "branch_checked_out_elsewhere",
                "Named branch is checked out in an incompatible worktree",
            )

    target_owner = next(
        (
            item
            for item in topology
            if _path_key(item.path) == _path_key(canonical_target)
        ),
        None,
    )
    if isolated and canonical_target.exists():
        if target_owner is None or target_owner.branch_ref != branch_ref:
            raise ClaimPlanError(
                "target_path_occupied",
                "Canonical target path is already owned by another artifact",
            )

    preconditions: list[ClaimPlanPrecondition] = [
        ClaimPlanPrecondition(
            "caller_head",
            str(caller_root),
            caller_head_sha,
        ),
        ClaimPlanPrecondition(
            "topology",
            str(canonical_root),
            _topology_digest(topology),
        ),
        ClaimPlanPrecondition("ref_oid", local_ref, local_sha),
        ClaimPlanPrecondition("ref_oid", selected_ref, selected_sha),
        ClaimPlanPrecondition(
            "ref_oid",
            branch_ref,
            branch_sha if branch_sha is not None else "absent",
        ),
        ClaimPlanPrecondition(
            "path",
            str(canonical_target),
            (
                f"worktree:{target_owner.branch_ref or 'detached'}"
                if target_owner is not None
                else "absent"
            ),
        ),
    ]
    if upstream_ref is not None and upstream_sha is not None:
        preconditions.append(
            ClaimPlanPrecondition("ref_oid", upstream_ref, upstream_sha)
        )
    if not isolated:
        preconditions.append(
            ClaimPlanPrecondition("caller_clean", str(caller_root), "clean")
        )

    return ClaimGitPlan(
        mode=mode,
        git_metadata_available=True,
        caller_path=str(caller_path),
        caller_worktree_path=str(caller_root),
        canonical_root=str(canonical_root),
        linked_worktree=linked,
        caller_dirty=caller_dirty,
        caller_head_ref=caller_head_ref,
        caller_head_sha=caller_head_sha,
        default_discovery=discovery,
        local_default_ref=local_ref,
        local_default_sha=local_sha,
        upstream_default_ref=upstream_ref,
        upstream_default_sha=upstream_sha,
        selected_default_base_ref=selected_ref,
        selected_default_base_sha=selected_sha,
        claim_start_ref=claim_start_ref,
        claim_start_sha=claim_start_sha,
        branch=planned_branch,
        branch_exists=branch_exists,
        branch_owner_path=branch_owner.path if branch_owner is not None else None,
        target_path=str(canonical_target),
        target_owner_branch=(
            target_owner.branch_ref if target_owner is not None else None
        ),
        worktrees=topology,
        warnings=tuple(warnings),
        revalidation_preconditions=tuple(preconditions),
    )


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


def tree_state(cwd: Path) -> str:
    """Read-only working-tree state for health probes (retro-opps T014).

    Returns ``"clean"``, ``"dirty"``, ``"not_a_repo"``, or ``"unavailable"``
    (git missing, timed out, or errored). Unlike :func:`_is_dirty` — whose
    timeout→dirty bias is right for REFUSING worktree creation — a probe
    must distinguish "verifiably dirty" from "could not check", so callers
    can warn on the former and stay informational on the latter.
    """
    if not is_git_available():
        return "unavailable"
    if not is_git_repo(cwd):
        return "not_a_repo"
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "unavailable"
    if result.returncode != 0:
        return "unavailable"
    return "dirty" if result.stdout.strip() else "clean"


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
