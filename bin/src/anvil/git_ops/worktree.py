"""Git worktree helpers for anvil claim flow.

Pure subprocess wrappers — no git Python library dependency.

All public functions return dataclasses rather than raising on git failures.
The CLI translates a created=False result into a one-line stderr warning.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
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
from anvil.state.models import ClaimGitMetadata

_MAX_GIT_OBSERVATION_BYTES = 8 * 1024 * 1024
_GIT_OBSERVATION_POLL_SECONDS = 0.01
_MAX_REF_TRANSACTION_OUTPUT_BYTES = 64 * 1024

__all__ = [
    "ClaimGitPlan",
    "ClaimGitMutation",
    "ClaimGitMutationTracker",
    "ClaimPlanError",
    "ClaimPlanPrecondition",
    "GitWorktree",
    "WorktreeResult",
    "apply_claim_plan",
    "canonical_git_root",
    "claim_git_metadata",
    "compensate_claim_plan",
    "compensate_claim_plan_tracker",
    "finalize_claim_plan_tracker",
    "create_worktree_for_task",
    "resolve_claim_plan",
    "revalidate_claim_plan",
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

    kind: Literal[
        "ref_oid",
        "caller_head",
        "caller_clean",
        "target_clean",
        "topology",
        "path",
    ]
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
    worktree_placement_root: str | None
    git_common_dir: str | None
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
    ignored_worktree_paths: tuple[str, ...]
    warnings: tuple[str, ...]
    revalidation_preconditions: tuple[ClaimPlanPrecondition, ...]


@dataclass(frozen=True)
class ClaimGitMutation:
    """External Git artifacts owned by one successfully applied claim plan."""

    plan: ClaimGitPlan
    branch_created: bool
    worktree_created: bool
    caller_checkout_changed: bool
    ownership_token: str | None = None
    branch_identity: tuple[int, int, int] | None = None
    branch_reflog_state: tuple[int, str] | None = None
    branch_marker_created: bool = False
    worktree_identity: tuple[int, int, int] | None = None
    checkout_identity: tuple[int, int, int] | None = None


@dataclass
class ClaimGitMutationTracker:
    """Positive ownership evidence updated only after a Git mutation succeeds."""

    plan: ClaimGitPlan
    ownership_token: str = dataclass_field(
        default_factory=lambda: secrets.token_hex(16)
    )
    branch_created: bool = False
    worktree_created: bool = False
    caller_checkout_changed: bool = False
    branch_identity: tuple[int, int, int] | None = None
    branch_reflog_state: tuple[int, str] | None = None
    branch_marker_created: bool = False
    worktree_identity: tuple[int, int, int] | None = None
    checkout_identity: tuple[int, int, int] | None = None


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    """Run a read-only Git probe with bounded stdout and stderr capture."""
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    command = ["git", *args]
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                env=env,
            )
            deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
            while process.poll() is None:
                total = os.fstat(stdout_file.fileno()).st_size + os.fstat(
                    stderr_file.fileno()
                ).st_size
                if total > _MAX_GIT_OBSERVATION_BYTES or time.monotonic() >= deadline:
                    process.kill()
                    process.wait()
                    return None
                time.sleep(_GIT_OBSERVATION_POLL_SECONDS)
            total = os.fstat(stdout_file.fileno()).st_size + os.fstat(
                stderr_file.fileno()
            ).st_size
            if total > _MAX_GIT_OBSERVATION_BYTES:
                return None
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(_MAX_GIT_OBSERVATION_BYTES + 1)
            stderr = stderr_file.read(_MAX_GIT_OBSERVATION_BYTES + 1)
            if len(stdout) + len(stderr) > _MAX_GIT_OBSERVATION_BYTES:
                return None
            return subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout.decode("utf-8", errors="strict"),
                stderr.decode("utf-8", errors="strict"),
            )
    except (OSError, UnicodeError):
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        return None
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise


def _git_value(args: list[str], cwd: Path, *, code: str) -> str:
    result = _run_git(args, cwd)
    if result is None:
        raise ClaimPlanError(code, "Git observation timed out or is unavailable")
    if result.returncode != 0:
        raise ClaimPlanError(code, "Git observation failed")
    return result.stdout.strip()


def _path_key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def _git_common_dir(cwd: Path) -> Path | None:
    result = _run_git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd,
    )
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve(strict=False)


def _git_dir(cwd: Path) -> Path | None:
    result = _run_git(
        ["rev-parse", "--path-format=absolute", "--git-dir"],
        cwd,
    )
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve(strict=False)


def canonical_git_root(cwd: Path) -> Path | None:
    """Return one stable repository identity shared by linked/nested callers."""
    if shutil.which("git") is None:
        return None
    common_dir = _git_common_dir(cwd)
    if common_dir is None:
        return None
    git_dir = _git_dir(cwd)
    root_result = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if (
        git_dir is None
        or root_result is None
        or root_result.returncode != 0
        or not root_result.stdout.strip()
    ):
        return None
    caller_root = Path(root_result.stdout.strip()).resolve(strict=False)
    core_worktree = _run_git(
        ["config", "--file", str(common_dir / "config"), "--get", "core.worktree"],
        cwd,
    )
    configured: Path | None = None
    if core_worktree is not None and core_worktree.returncode == 0:
        raw_path = Path(core_worktree.stdout.strip())
        candidate = (
            raw_path if raw_path.is_absolute() else common_dir / raw_path
        ).resolve(strict=False)
        if candidate.is_dir():
            configured = candidate
    if _path_key(git_dir) == _path_key(common_dir):
        if _path_key(common_dir.parent) == _path_key(caller_root):
            return caller_root
        if configured is not None and _path_key(configured) == _path_key(caller_root):
            return caller_root
        # A separate-git-dir repository does not record its main checkout.
        # The common directory is the only identity discoverable from every
        # linked caller, so use it consistently rather than caller-local paths.
        return common_dir
    try:
        topology = _worktree_topology(cwd)
    except ClaimPlanError:
        return None
    if not topology:
        return None
    registered = {_path_key(item.path): Path(item.path) for item in topology}
    conventional = common_dir.parent
    if _path_key(conventional) in registered:
        return conventional.resolve(strict=False)

    # Absorbed submodules store their common directory below the superproject's
    # ``.git/modules`` tree. Their config's core.worktree points back to the
    # actual submodule checkout; git-common-dir.parent does not.
    if configured is not None:
        return configured
    # ``git init --separate-git-dir`` does not persist the main checkout path.
    # From a linked worktree Git reports the common metadata directory as the
    # first topology entry, so use that stable coordination root rather than
    # inventing or searching for an unrecorded checkout path.
    if _path_key(common_dir) in registered:
        return common_dir
    return None


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


def _normalize_topology_root(
    topology: tuple[GitWorktree, ...],
    *,
    canonical_root: Path | None,
    common_dir: Path | None,
) -> tuple[GitWorktree, ...]:
    """Project an absorbed submodule's common-dir record to its checkout."""
    if canonical_root is None or common_dir is None:
        return topology
    if any(_path_key(item.path) == _path_key(canonical_root) for item in topology):
        return topology
    return tuple(
        replace(item, path=str(canonical_root))
        if _path_key(item.path) == _path_key(common_dir)
        else item
        for item in topology
    )


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
    ignored_worktree_paths: tuple[Path, ...] = (),
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
            worktree_placement_root=None,
            git_common_dir=None,
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
            ignored_worktree_paths=(),
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
    common_dir = _git_common_dir(caller_path)
    caller_git_dir = _git_dir(caller_path)
    if common_dir is None or caller_git_dir is None:
        raise ClaimPlanError(
            "caller_topology_unavailable",
            "Git directory identity cannot be resolved",
        )
    linked = _path_key(caller_git_dir) != _path_key(common_dir)
    separate_git_dir = _path_key(canonical_root) == _path_key(common_dir)
    if separate_git_dir and linked and isolated:
        raise ClaimPlanError(
            "worktree_placement_unavailable",
            "A linked separate-git-dir caller cannot resolve the main checkout placement",
        )
    worktree_placement_root = caller_root if separate_git_dir else canonical_root
    normalized_ignored_paths = tuple(
        str(path.resolve(strict=False)) for path in ignored_worktree_paths
    )
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
    caller_dirty = _working_tree_dirty(
        caller_root,
        ignored_paths=normalized_ignored_paths,
    )
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

    topology = _normalize_topology_root(
        _worktree_topology(caller_path),
        canonical_root=canonical_root,
        common_dir=common_dir,
    )
    caller_record = next(
        (
            item
            for item in topology
            if _path_key(item.path)
            == _path_key(caller_root if linked else canonical_root)
        ),
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
            else worktree_placement_root.parent / target_path
        )
        canonical_target = target_candidate.resolve(strict=False)
    else:
        canonical_target = None if isolated else caller_root
    if isolated and canonical_target is None:
        canonical_target = (
            worktree_placement_root.parent
            / f"wt-{safe_path_component(task_id).lower()}"
        ).resolve(strict=False)

    if isolated:
        _validate_isolated_target(worktree_placement_root, canonical_target)

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
    elif target_owner is not None:
        target_dirty = _working_tree_dirty(Path(target_owner.path))
        if target_dirty:
            raise ClaimPlanError(
                "target_worktree_dirty",
                "An existing claim worktree must be clean before reuse",
            )
        preconditions.append(
            ClaimPlanPrecondition("target_clean", target_owner.path, "clean")
        )

    return ClaimGitPlan(
        mode=mode,
        git_metadata_available=True,
        caller_path=str(caller_path),
        caller_worktree_path=str(caller_root),
        canonical_root=str(canonical_root),
        worktree_placement_root=str(worktree_placement_root),
        git_common_dir=str(common_dir),
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
        ignored_worktree_paths=normalized_ignored_paths,
        warnings=tuple(warnings),
        revalidation_preconditions=tuple(preconditions),
    )


def claim_git_metadata(plan: ClaimGitPlan) -> ClaimGitMetadata | None:
    """Project a Git-backed plan into the durable state contract."""
    if not plan.git_metadata_available:
        return None
    required = {
        "canonical_root": plan.canonical_root,
        "selected_default_base_ref": plan.selected_default_base_ref,
        "selected_default_base_sha": plan.selected_default_base_sha,
        "claim_start_ref": plan.claim_start_ref,
        "claim_start_sha": plan.claim_start_sha,
        "branch": plan.branch,
        "target_path": plan.target_path,
    }
    if any(value is None for value in required.values()):
        raise ClaimPlanError("claim_plan_incomplete", "Git claim plan is incomplete")
    return ClaimGitMetadata(
        mode=plan.mode,
        canonical_root=str(required["canonical_root"]),
        selected_default_base_ref=str(required["selected_default_base_ref"]),
        selected_default_base_sha=str(required["selected_default_base_sha"]),
        claim_start_ref=str(required["claim_start_ref"]),
        claim_start_sha=str(required["claim_start_sha"]),
        branch=str(required["branch"]),
        target_path=str(required["target_path"]),
        worktree_path=plan.target_path if plan.mode == "isolated" else None,
    )


def revalidate_claim_plan(plan: ClaimGitPlan, *, cwd: Path | None = None) -> None:
    """Refuse when any exact T011 observation changed after planning."""
    if not plan.git_metadata_available:
        return
    root = Path(cwd or plan.caller_path)
    topology: tuple[GitWorktree, ...] | None = None
    for condition in plan.revalidation_preconditions:
        if condition.kind == "ref_oid":
            actual = _ref_oid(condition.subject, root)
            value = actual if actual is not None else "absent"
        elif condition.kind == "caller_head":
            value = _git_value(
                ["rev-parse", "--verify", "HEAD^{commit}"],
                Path(condition.subject),
                code="claim_plan_changed",
            )
        elif condition.kind in {"caller_clean", "target_clean"}:
            ignored_paths = (
                plan.ignored_worktree_paths
                if condition.kind == "caller_clean"
                else ()
            )
            value = (
                "dirty"
                if _working_tree_dirty(
                    Path(condition.subject), ignored_paths=ignored_paths
                )
                else "clean"
            )
        elif condition.kind == "topology":
            topology = topology or _normalize_topology_root(
                _worktree_topology(root),
                canonical_root=(
                    Path(plan.canonical_root)
                    if plan.canonical_root is not None
                    else None
                ),
                common_dir=(
                    Path(plan.git_common_dir)
                    if plan.git_common_dir is not None
                    else None
                ),
            )
            value = _topology_digest(topology)
        else:
            topology = topology or _normalize_topology_root(
                _worktree_topology(root),
                canonical_root=(
                    Path(plan.canonical_root)
                    if plan.canonical_root is not None
                    else None
                ),
                common_dir=(
                    Path(plan.git_common_dir)
                    if plan.git_common_dir is not None
                    else None
                ),
            )
            owner = next(
                (
                    item
                    for item in topology
                    if _path_key(item.path) == _path_key(condition.subject)
                ),
                None,
            )
            if owner is not None:
                value = f"worktree:{owner.branch_ref or 'detached'}"
            elif Path(condition.subject).exists():
                value = "occupied"
            else:
                value = "absent"
        if value != condition.expected:
            raise ClaimPlanError(
                "claim_plan_changed",
                "Git claim preconditions changed after planning",
            )


def apply_claim_plan(
    plan: ClaimGitPlan,
    *,
    cwd: Path | None = None,
    tracker: ClaimGitMutationTracker | None = None,
) -> ClaimGitMutation:
    """Apply one revalidated plan and roll back only artifacts created here."""
    ownership = tracker or ClaimGitMutationTracker(plan)
    if ownership.plan != plan:
        raise ClaimPlanError("claim_plan_mismatch", "Mutation tracker has another plan")
    if not plan.git_metadata_available:
        return ClaimGitMutation(plan, False, False, False)
    root = Path(cwd or plan.caller_path)
    revalidate_claim_plan(plan, cwd=root)
    if plan.branch is None or plan.claim_start_sha is None:
        raise ClaimPlanError("claim_plan_incomplete", "Git claim plan is incomplete")
    branch_ref = f"refs/heads/{plan.branch}"
    marker_ref = _branch_marker_ref(ownership.ownership_token)

    def record_marker() -> None:
        if _ref_oid(marker_ref, root) != plan.claim_start_sha:
            raise ClaimPlanError(
                "mutation_ownership_unavailable",
                "Branch ownership marker cannot be proven",
            )
        ownership.branch_marker_created = True

    def record_branch() -> None:
        identity = _branch_identity(plan)
        reflog_state = _branch_reflog_state(plan, root)
        if (
            identity is None
            and reflog_state is None
            and not _branch_marker_matches(ownership.ownership_token, plan, root)
        ):
            raise ClaimPlanError(
                "mutation_ownership_unavailable",
                "Created branch ownership cannot be proven",
            )
        ownership.branch_created = True
        ownership.branch_identity = identity
        ownership.branch_reflog_state = reflog_state

    def record_checkout() -> None:
        identity = _checkout_identity(plan, root)
        if identity is None:
            raise ClaimPlanError(
                "mutation_ownership_unavailable",
                "Checkout ownership cannot be proven",
            )
        ownership.caller_checkout_changed = True
        ownership.checkout_identity = identity

    def record_worktree() -> None:
        identity = _worktree_identity(plan)
        if identity is None:
            raise ClaimPlanError(
                "mutation_ownership_unavailable",
                "Created worktree ownership cannot be proven",
            )
        ownership.worktree_created = True
        ownership.worktree_identity = identity

    try:
        if not plan.branch_exists:
            _mutate_git(
                ["update-ref", marker_ref, plan.claim_start_sha, ""],
                root,
                code="branch_marker_create_failed",
                on_success=record_marker,
                success_probe=lambda: _ref_oid(marker_ref, root)
                == plan.claim_start_sha,
            )
            branch_action = _ownership_action(ownership.ownership_token, "branch")
            _mutate_git(
                [
                    "update-ref",
                    "--create-reflog",
                    "-m",
                    branch_action,
                    branch_ref,
                    plan.claim_start_sha,
                    "",
                ],
                root,
                code="branch_create_failed",
                on_success=record_branch,
                success_probe=lambda: _reflog_message(branch_ref, root)
                == branch_action,
            )
        if _ref_oid(branch_ref, root) != plan.claim_start_sha:
            raise ClaimPlanError(
                "branch_moved", "Claim branch no longer identifies the planned start"
            )

        if plan.mode == "shared":
            current_ref = _symbolic_head(root)
            current_sha = _git_value(
                ["rev-parse", "--verify", "HEAD^{commit}"],
                root,
                code="caller_head_unavailable",
            )
            if current_ref != branch_ref or current_sha != plan.claim_start_sha:
                checkout_action = _ownership_action(
                    ownership.ownership_token, "checkout"
                )
                _mutate_git(
                    ["checkout", "--no-guess", plan.branch],
                    root,
                    code="branch_checkout_failed",
                    on_success=record_checkout,
                    success_probe=lambda: _reflog_message("HEAD", root)
                    == checkout_action,
                    reflog_action=checkout_action,
                )
        elif plan.mode == "isolated":
            if (
                plan.canonical_root is None
                or plan.worktree_placement_root is None
                or plan.target_path is None
            ):
                raise ClaimPlanError("claim_plan_incomplete", "Worktree plan is incomplete")
            target = Path(plan.target_path)
            placement_root = Path(plan.worktree_placement_root)
            _validate_isolated_target(placement_root, target)
            topology = _worktree_topology(root)
            owner = next(
                (item for item in topology if _path_key(item.path) == _path_key(target)),
                None,
            )
            if owner is None:
                if target.exists():
                    raise ClaimPlanError(
                        "target_path_occupied", "Claim worktree target is occupied"
                    )
                worktree_action = _ownership_action(
                    ownership.ownership_token, "worktree"
                )
                _mutate_git(
                    ["worktree", "add", "--no-guess", str(target), plan.branch],
                    root,
                    code="worktree_create_failed",
                    on_success=record_worktree,
                    success_probe=lambda: (
                        _reflog_message("HEAD", target) or ""
                    ).startswith(worktree_action),
                    reflog_action=worktree_action,
                )
            _validate_isolated_target(placement_root, target, must_exist=True)
            owner = next(
                (
                    item
                    for item in _worktree_topology(root)
                    if _path_key(item.path) == _path_key(target)
                ),
                None,
            )
            if (
                owner is None
                or owner.branch_ref != branch_ref
                or owner.head_sha != plan.claim_start_sha
            ):
                raise ClaimPlanError(
                    "worktree_verification_failed",
                    "Created worktree does not match the claim plan",
                )
        mutation = ClaimGitMutation(
            plan=plan,
            branch_created=ownership.branch_created,
            worktree_created=ownership.worktree_created,
            caller_checkout_changed=ownership.caller_checkout_changed,
            ownership_token=ownership.ownership_token,
            branch_identity=ownership.branch_identity,
            branch_reflog_state=ownership.branch_reflog_state,
            branch_marker_created=ownership.branch_marker_created,
            worktree_identity=ownership.worktree_identity,
            checkout_identity=ownership.checkout_identity,
        )
        if _ref_oid(branch_ref, root) != plan.claim_start_sha:
            raise ClaimPlanError("branch_moved", "Claim branch moved during mutation")
        return mutation
    except BaseException:
        compensate_claim_plan_tracker(ownership, cwd=root)
        raise


def compensate_claim_plan(
    mutation: ClaimGitMutation,
    *,
    cwd: Path | None = None,
) -> None:
    """Remove artifacts with retained invocation ownership evidence."""
    _compensate_values(
        mutation.plan,
        branch_created=mutation.branch_created,
        worktree_created=mutation.worktree_created,
        caller_checkout_changed=mutation.caller_checkout_changed,
        ownership_token=mutation.ownership_token,
        branch_identity=mutation.branch_identity,
        branch_reflog_state=mutation.branch_reflog_state,
        branch_marker_created=mutation.branch_marker_created,
        worktree_identity=mutation.worktree_identity,
        checkout_identity=mutation.checkout_identity,
        cwd=Path(cwd or mutation.plan.caller_path),
    )


def compensate_claim_plan_tracker(
    tracker: ClaimGitMutationTracker,
    *,
    cwd: Path | None = None,
) -> None:
    """Compensate only mutations positively recorded by this invocation."""
    plan = tracker.plan
    if not plan.git_metadata_available or plan.branch is None:
        return
    root = Path(cwd or plan.caller_path)
    _compensate_values(
        plan,
        branch_created=tracker.branch_created,
        worktree_created=tracker.worktree_created,
        caller_checkout_changed=tracker.caller_checkout_changed,
        ownership_token=tracker.ownership_token,
        branch_identity=tracker.branch_identity,
        branch_reflog_state=tracker.branch_reflog_state,
        branch_marker_created=tracker.branch_marker_created,
        worktree_identity=tracker.worktree_identity,
        checkout_identity=tracker.checkout_identity,
        cwd=root,
    )
    tracker.branch_created = False
    tracker.worktree_created = False
    tracker.caller_checkout_changed = False
    tracker.branch_identity = None
    tracker.branch_reflog_state = None
    tracker.branch_marker_created = False
    tracker.worktree_identity = None
    tracker.checkout_identity = None


def _compensate_values(
    plan: ClaimGitPlan,
    *,
    branch_created: bool,
    worktree_created: bool,
    caller_checkout_changed: bool,
    ownership_token: str | None = None,
    branch_identity: tuple[int, int, int] | None = None,
    branch_reflog_state: tuple[int, str] | None = None,
    branch_marker_created: bool = False,
    worktree_identity: tuple[int, int, int] | None = None,
    checkout_identity: tuple[int, int, int] | None = None,
    cwd: Path,
) -> None:
    if not plan.git_metadata_available or plan.branch is None or plan.claim_start_sha is None:
        return
    branch_ref = f"refs/heads/{plan.branch}"
    caller = Path(plan.caller_worktree_path or cwd)

    def owns_branch() -> bool:
        return _branch_ownership_matches(
            plan,
            cwd=cwd,
            ownership_token=ownership_token,
            branch_identity=branch_identity,
            branch_reflog_state=branch_reflog_state,
            branch_marker_created=branch_marker_created,
        )

    def clean_owned_artifacts_while_locked() -> bool:
        if worktree_created:
            if (
                plan.target_path is None
                or worktree_identity is None
                or _worktree_identity(plan) != worktree_identity
            ):
                return False
            owner = next(
                (
                    item
                    for item in _worktree_topology(cwd)
                    if _path_key(item.path) == _path_key(plan.target_path)
                ),
                None,
            )
            if (
                owner is None
                or owner.branch_ref != branch_ref
                or owner.head_sha != plan.claim_start_sha
                or _working_tree_dirty(Path(plan.target_path))
            ):
                return False
            try:
                # Let Git perform its own final dirtiness check at removal
                # time. A file created after our bounded observation must make
                # cleanup refuse, never be erased by `--force`.
                _mutate_git(
                    ["worktree", "remove", plan.target_path],
                    cwd,
                    code="worktree_compensation_failed",
                )
            except ClaimPlanError:
                return False

        return not any(
            item.branch_ref == branch_ref for item in _worktree_topology(cwd)
        )

    def restore_owned_checkout() -> None:
        owns_checkout = bool(
            checkout_identity is not None
            and _checkout_identity(plan, cwd) == checkout_identity
        )
        if caller_checkout_changed and owns_checkout:
            if plan.caller_head_ref is not None:
                restore_name = plan.caller_head_ref.removeprefix("refs/heads/")
                _mutate_git(
                    ["checkout", "--no-guess", restore_name],
                    caller,
                    code="checkout_compensation_failed",
                )
            elif plan.caller_head_sha is not None:
                _mutate_git(
                    ["checkout", "--detach", plan.caller_head_sha],
                    caller,
                    code="checkout_compensation_failed",
                )

    if branch_created and _ref_oid(branch_ref, cwd) == plan.claim_start_sha:
        # Checkout needs to update HEAD while it still points at the claim
        # branch, which Git refuses once that branch ref is transaction-locked.
        # Restore it first; the prepared delete below then closes the destructive
        # same-SHA replacement window for the worktree/ref cleanup itself.
        if owns_branch():
            restore_owned_checkout()
        _delete_owned_branch_transaction(
            plan,
            cwd=cwd,
            owner_check=owns_branch,
            on_locked=clean_owned_artifacts_while_locked,
        )
    elif owns_branch():
        restore_owned_checkout()
        clean_owned_artifacts_while_locked()
    if branch_marker_created and ownership_token is not None:
        _remove_branch_marker(ownership_token, plan, cwd)


def _branch_ownership_matches(
    plan: ClaimGitPlan,
    *,
    cwd: Path,
    ownership_token: str | None,
    branch_identity: tuple[int, int, int] | None,
    branch_reflog_state: tuple[int, str] | None,
    branch_marker_created: bool,
) -> bool:
    current_reflog_state = _branch_reflog_state(plan, cwd)
    current_reflog_count = _branch_reflog_entry_count(plan, cwd)
    current_branch_identity = _branch_identity(plan)
    marker_matches = bool(
        branch_marker_created
        and ownership_token is not None
        and _branch_marker_matches(ownership_token, plan, cwd)
    )
    if branch_reflog_state is not None and current_reflog_state is not None:
        return current_reflog_state == branch_reflog_state
    if branch_reflog_state is not None and current_reflog_count not in (None, 0):
        # A recreated branch can have a fresh reflog that lacks a parseable
        # message. Its non-zero entry count is still a continuity mismatch.
        return False
    if branch_identity is not None and current_branch_identity is not None:
        # A still-loose ref is an independent continuity witness. Preserve a
        # replacement whenever its filesystem identity differs, even if the
        # repository disables ordinary reflog updates.
        return current_branch_identity == branch_identity
    if branch_reflog_state is not None:
        # Normal packing/reftable maintenance removes the loose-ref witness;
        # normal reflog expiry leaves an exact zero-entry observation. Only
        # then may the invocation's durable marker carry ownership forward.
        return current_reflog_count == 0 and marker_matches
    return marker_matches


def _delete_owned_branch_transaction(
    plan: ClaimGitPlan,
    *,
    cwd: Path,
    owner_check: Callable[[], bool],
    on_locked: Callable[[], bool],
) -> bool:
    """Delete an owned branch only after lock-scoped continuity revalidation."""
    if plan.branch is None or plan.claim_start_sha is None:
        return False
    branch_ref = f"refs/heads/{plan.branch}"
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    process: subprocess.Popen[bytes] | None = None
    prepared = False
    with tempfile.TemporaryDirectory(prefix="anvil-ref-transaction-") as temp:
        stdout_path = Path(temp) / "stdout"
        stderr_path = Path(temp) / "stderr"
        try:
            with stdout_path.open("wb") as stdout_file, stderr_path.open(
                "wb"
            ) as stderr_file:
                process = subprocess.Popen(
                    ["git", "update-ref", "--stdin"],
                    cwd=str(cwd),
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env=env,
                )
            if process.stdin is None:
                raise ClaimPlanError(
                    "branch_compensation_failed", "Git ref transaction is unavailable"
                )
            request = (
                "start\n"
                f"delete {branch_ref} {plan.claim_start_sha}\n"
                "prepare\n"
            ).encode("ascii")
            process.stdin.write(request)
            process.stdin.flush()
            prepared = _wait_for_ref_transaction_prepare(
                process, stdout_path=stdout_path, stderr_path=stderr_path
            )
            if not prepared:
                return False
            if not owner_check() or not on_locked():
                _finish_ref_transaction(
                    process,
                    command=b"abort\n",
                    expected=b"abort: ok\n",
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )
                prepared = False
                return False
            _finish_ref_transaction(
                process,
                command=b"commit\n",
                expected=b"commit: ok\n",
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            prepared = False
            return True
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            raise ClaimPlanError(
                "branch_compensation_failed", "Git ref transaction failed"
            ) from exc
        finally:
            if process is not None and process.poll() is None:
                if prepared and process.stdin is not None:
                    try:
                        process.stdin.write(b"abort\n")
                        process.stdin.flush()
                    except OSError:
                        pass
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def _wait_for_ref_transaction_prepare(
    process: subprocess.Popen[bytes],
    *,
    stdout_path: Path,
    stderr_path: Path,
) -> bool:
    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _ref_transaction_output_too_large(stdout_path, stderr_path):
            raise ClaimPlanError(
                "branch_compensation_failed", "Git ref transaction output is too large"
            )
        output = stdout_path.read_bytes() if stdout_path.exists() else b""
        if b"prepare: ok\n" in output:
            return True
        if process.poll() is not None:
            return False
        time.sleep(_GIT_OBSERVATION_POLL_SECONDS)
    raise subprocess.TimeoutExpired(process.args, _GIT_TIMEOUT_SECONDS)


def _finish_ref_transaction(
    process: subprocess.Popen[bytes],
    *,
    command: bytes,
    expected: bytes,
    stdout_path: Path,
    stderr_path: Path,
) -> None:
    if process.stdin is None:
        raise ClaimPlanError(
            "branch_compensation_failed", "Git ref transaction input is unavailable"
        )
    process.stdin.write(command)
    process.stdin.flush()
    process.stdin.close()
    process.wait(timeout=_GIT_TIMEOUT_SECONDS)
    if _ref_transaction_output_too_large(stdout_path, stderr_path):
        raise ClaimPlanError(
            "branch_compensation_failed", "Git ref transaction output is too large"
        )
    output = stdout_path.read_bytes() if stdout_path.exists() else b""
    if process.returncode != 0 or expected not in output:
        raise ClaimPlanError(
            "branch_compensation_failed", "Git ref transaction was refused"
        )


def _ref_transaction_output_too_large(stdout_path: Path, stderr_path: Path) -> bool:
    try:
        return (
            stdout_path.stat().st_size + stderr_path.stat().st_size
            > _MAX_REF_TRANSACTION_OUTPUT_BYTES
        )
    except OSError:
        return True


def finalize_claim_plan_tracker(
    tracker: ClaimGitMutationTracker,
    *,
    cwd: Path | None = None,
) -> bool:
    """Best-effort retirement after state and Git claim are already durable."""
    if tracker.branch_marker_created:
        try:
            _remove_branch_marker(
                tracker.ownership_token,
                tracker.plan,
                Path(cwd or tracker.plan.caller_path),
            )
        except ClaimPlanError:
            return False
    tracker.branch_marker_created = False
    return True


def _working_tree_dirty(
    cwd: Path,
    *,
    ignored_paths: tuple[str, ...] = (),
) -> bool:
    root = cwd.resolve(strict=False)
    exclusions: list[str] = []
    for raw_path in ignored_paths:
        try:
            relative = Path(raw_path).resolve(strict=False).relative_to(root)
        except ValueError:
            continue
        portable = relative.as_posix()
        if portable in {"", "."}:
            continue
        exclusions.extend(
            [f":(exclude,top){portable}", f":(exclude,top){portable}/**"]
        )
    args = ["status", "--porcelain=v1", "--untracked-files=all"]
    if exclusions:
        args.extend(["--", ".", *exclusions])
    result = _run_git(args, root)
    if result is None or result.returncode != 0:
        raise ClaimPlanError("caller_dirty_unavailable", "Worktree dirtiness is unknown")
    return bool(result.stdout)


def _symbolic_head(cwd: Path) -> str | None:
    result = _run_git(["symbolic-ref", "--quiet", "HEAD"], cwd)
    if result is None:
        raise ClaimPlanError("caller_head_unavailable", "Caller HEAD is unavailable")
    return result.stdout.strip() if result.returncode == 0 else None


def _ownership_action(token: str, kind: str) -> str:
    return f"anvil-claim:{token}:{kind}"


def _branch_marker_ref(token: str) -> str:
    return f"refs/anvil/claim-ownership/{token}"


def _branch_marker_matches(token: str, plan: ClaimGitPlan, cwd: Path) -> bool:
    return bool(
        plan.claim_start_sha is not None
        and _ref_oid(_branch_marker_ref(token), cwd) == plan.claim_start_sha
    )


def _remove_branch_marker(token: str, plan: ClaimGitPlan, cwd: Path) -> None:
    if plan.claim_start_sha is None:
        return
    marker_ref = _branch_marker_ref(token)
    if _ref_oid(marker_ref, cwd) != plan.claim_start_sha:
        return
    _mutate_git(
        ["update-ref", "-d", marker_ref, plan.claim_start_sha],
        cwd,
        code="branch_marker_cleanup_failed",
    )


def _artifact_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return None
    return (int(info.st_dev), int(info.st_ino), int(info.st_ctime_ns))


def _branch_identity(plan: ClaimGitPlan) -> tuple[int, int, int] | None:
    if plan.git_common_dir is None or plan.branch is None:
        return None
    identity = _artifact_identity(
        Path(plan.git_common_dir) / "refs" / "heads" / plan.branch
    )
    return (identity[0], identity[1], 0) if identity is not None else None


def _branch_reflog_state(plan: ClaimGitPlan, cwd: Path) -> tuple[int, str] | None:
    if plan.branch is None:
        return None
    branch_ref = f"refs/heads/{plan.branch}"
    message = _reflog_message(branch_ref, cwd)
    count_result = _run_git(
        ["rev-list", "--walk-reflogs", "--count", branch_ref], cwd
    )
    if (
        not message
        or count_result is None
        or count_result.returncode != 0
        or not count_result.stdout.strip().isdigit()
    ):
        return None
    count = int(count_result.stdout.strip())
    return (count, message) if count > 0 else None


def _branch_reflog_entry_count(plan: ClaimGitPlan, cwd: Path) -> int | None:
    """Return the current branch reflog entry count, including zero."""
    if plan.branch is None:
        return None
    result = _run_git(
        ["rev-list", "--walk-reflogs", "--count", f"refs/heads/{plan.branch}"],
        cwd,
    )
    if (
        result is None
        or result.returncode != 0
        or not result.stdout.strip().isdigit()
    ):
        return None
    return int(result.stdout.strip())


def _worktree_identity(plan: ClaimGitPlan) -> tuple[int, int, int] | None:
    if plan.target_path is None:
        return None
    git_dir = _git_dir(Path(plan.target_path))
    identity = _artifact_identity(git_dir) if git_dir is not None else None
    # POSIX ctime changes when Git maintains files inside the administrative
    # directory (notably `reflog expire`). Device + inode identify the same
    # directory across that maintenance; branch continuity is checked
    # independently before any worktree removal.
    return (identity[0], identity[1], 0) if identity is not None else None


def _checkout_identity(plan: ClaimGitPlan, cwd: Path) -> tuple[int, int, int] | None:
    git_dir = _git_dir(Path(plan.caller_worktree_path or cwd))
    return _artifact_identity(git_dir / "HEAD") if git_dir is not None else None


def _reflog_message(ref: str, cwd: Path) -> str | None:
    result = _run_git(["reflog", "show", "-1", "--format=%gs", ref], cwd)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip()


def _mutate_git(
    args: list[str],
    cwd: Path,
    *,
    code: str,
    on_success: Callable[[], None] | None = None,
    success_probe: Callable[[], bool] | None = None,
    reflog_action: str | None = None,
) -> None:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if reflog_action is not None:
        env["GIT_REFLOG_ACTION"] = reflog_action
        # HEAD/worktree reflogs are optional repository policy. Ownership
        # markers must remain durable even when core.logAllRefUpdates=false;
        # override only this child process and never mutate repository config.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "core.logAllRefUpdates"
        env["GIT_CONFIG_VALUE_0"] = "always"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise ClaimPlanError(code, "Git mutation failed")
        if on_success is not None:
            on_success()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaimPlanError(code, "Git mutation timed out or is unavailable") from exc
    except ClaimPlanError:
        raise
    except BaseException:
        if success_probe is not None and success_probe() and on_success is not None:
            on_success()
        raise


def _validate_isolated_target(
    canonical_root: Path,
    target: Path,
    *,
    must_exist: bool = False,
) -> None:
    placement = canonical_root.resolve(strict=True).parent
    candidate = target.resolve(strict=False)
    try:
        common = Path(os.path.commonpath((str(placement), str(candidate))))
    except ValueError as exc:
        raise ClaimPlanError(
            "target_outside_root", "Claim target is outside its placement root"
        ) from exc
    if (
        _path_key(common) != _path_key(placement)
        or _path_key(candidate) == _path_key(placement)
    ):
        raise ClaimPlanError("target_outside_root", "Claim target is outside its placement root")
    relative = candidate.relative_to(placement)
    current = placement
    for part in relative.parts:
        if current.exists():
            matches = [
                entry
                for entry in current.iterdir()
                if entry.name.casefold() == part.casefold()
            ]
            if matches and all(entry.name != part for entry in matches):
                raise ClaimPlanError(
                    "target_case_collision", "Claim target has a case-insensitive collision"
                )
        current = current / part
        if current.exists() or current.is_symlink():
            try:
                info = current.lstat()
            except OSError as exc:
                raise ClaimPlanError(
                    "target_path_unavailable", "Claim target is unavailable"
                ) from exc
            attributes = getattr(info, "st_file_attributes", 0)
            if stat.S_ISLNK(info.st_mode) or bool(
                attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise ClaimPlanError(
                    "target_reparse_point", "Claim target cannot traverse a reparse point"
                )
    if must_exist and not candidate.is_dir():
        raise ClaimPlanError("target_path_unavailable", "Claim target worktree is unavailable")


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
