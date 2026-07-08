"""``anvil merge-check`` — pre-merge freshness + merged-tree verification.

retro-opps:T006. The consumer of :mod:`anvil.git_ops.freshness`: resolves the
task's claim branch, reports how far it sits behind the integration base and
whether it textually conflicts, and — with ``--run-checks`` — builds a
throwaway detached worktree of the WOULD-BE MERGE RESULT and runs the task's
verification commands inside it, catching the semantic drift a clean textual
merge can hide.

The user's working tree and current branch are never touched: the merged tree
is materialized via ``git merge-tree --write-tree`` + ``git commit-tree``
(object-db only) and checked out into a temporary worktree under the state
dir, removed in a ``finally`` even when a check fails.

Exit contract: 0 when the branch is fresh (or the freshness probe degraded —
offline is not a failure; strict gating lives in the T007 apply knob),
1 when the branch is verifiably stale, textually conflicted, or any
merged-tree check exits non-zero.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from anvil.cli._helpers import _resolve_state_dir
from anvil.cli._json import JSON_OPTION, emit_success, fail
from anvil.git_ops.branch import _GIT_TIMEOUT_SECONDS
from anvil.git_ops.freshness import FreshnessReport, check_freshness, resolve_base
from anvil.naming import safe_path_component

_COMMAND = "merge-check"

# Generous per-command budget: verification commands are test suites, not
# probes. A hung command must not wedge the CLI forever.
_CHECK_TIMEOUT_SECONDS = 600


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
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


def _branch_for_task(
    backend,  # noqa: ANN001
    task_id: str,
    project_root: Path,
) -> str | None:
    """Resolve the task's working branch.

    Preference order: the most recent claim row carrying a branch (any
    status — ``submit`` auto-releases before review, and the recorded name
    outlives the claim), then a ref scan for the deterministic
    ``<prefix>/<safe_task_id>-<slug>`` name ``create_branch_for_task``
    builds (the claim CLI does not currently persist the branch on the
    claim row, so the scan is the working path today). Newest commit wins
    when several match (collision ``-2``/``-3`` suffixes).
    """
    claims = [
        c
        for c in backend.list_claims()
        if c.task_id == task_id and c.branch
    ]
    if claims:
        claims.sort(key=lambda c: c.created_at, reverse=True)
        return claims[0].branch

    safe_id = safe_path_component(task_id).lower()
    for pattern in (
        f"refs/heads/*/{safe_id}-*",  # "<prefix>/<id>-<slug>"
        f"refs/heads/{safe_id}-*",    # empty branch_prefix
    ):
        refs = _git(
            [
                "for-each-ref",
                "--format=%(refname:short)",
                "--sort=-committerdate",
                pattern,
            ],
            project_root,
        )
        if refs is not None and refs.returncode == 0 and refs.stdout.strip():
            return refs.stdout.strip().splitlines()[0]
    return None


def _merged_tree_commit(
    base_ref: str, branch: str, cwd: Path
) -> tuple[str | None, str | None]:
    """Build a commit of the merged tree in the object db; never touches the
    working tree. Returns (commit_oid, error_reason)."""
    tree = _git(["merge-tree", "--write-tree", base_ref, branch], cwd)
    if tree is None:
        return None, "merge-tree timed out or git missing"
    if tree.returncode != 0:
        # Conflicts (exit 1) were already surfaced by the freshness report;
        # anything else is an environment problem.
        detail = tree.stderr.strip() or f"exit {tree.returncode}"
        return None, f"merged tree not buildable: {detail}"
    tree_oid = tree.stdout.strip().splitlines()[0]
    commit = _git(
        [
            "commit-tree",
            tree_oid,
            "-p",
            base_ref,
            "-p",
            branch,
            "-m",
            "anvil merge-check throwaway merge",
        ],
        cwd,
    )
    if commit is None or commit.returncode != 0:
        detail = (
            "timed out"
            if commit is None
            else (commit.stderr.strip() or f"exit {commit.returncode}")
        )
        return None, f"commit-tree failed: {detail}"
    return commit.stdout.strip(), None


def _run_merged_tree_checks(
    commands: list[str],
    commit_oid: str,
    *,
    repo_root: Path,
    worktree_path: Path,
) -> tuple[list[dict[str, object]], str | None]:
    """Check out *commit_oid* into a throwaway worktree and run *commands*.

    The worktree is removed in a ``finally`` even when a command fails or
    times out. Commands run with ``shell=True`` — they are the task's own
    committed ``verification.commands``, the same trust boundary as a
    workflow proof or CI step (see run_workflow.py) — from the worktree root.
    """
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    added = _git(
        ["worktree", "add", "--detach", str(worktree_path), commit_oid],
        repo_root,
    )
    if added is None or added.returncode != 0:
        detail = (
            "timed out"
            if added is None
            else (added.stderr.strip() or f"exit {added.returncode}")
        )
        return [], f"could not create merged-tree worktree: {detail}"

    checks: list[dict[str, object]] = []
    try:
        for command in commands:
            try:
                result = subprocess.run(  # noqa: S602 — committed task commands
                    command,
                    shell=True,
                    cwd=str(worktree_path),
                    capture_output=True,
                    text=True,
                    timeout=_CHECK_TIMEOUT_SECONDS,
                )
                exit_code: int | None = result.returncode
                detail = None
            except subprocess.TimeoutExpired:
                exit_code = None
                detail = f"timed out after {_CHECK_TIMEOUT_SECONDS}s"
            checks.append(
                {"command": command, "exit_code": exit_code, "detail": detail}
            )
        return checks, None
    finally:
        _git(
            ["worktree", "remove", "--force", str(worktree_path)], repo_root
        )


def _json_data(
    task_id: str,
    branch: str,
    report: FreshnessReport,
    checks: list[dict[str, object]],
    checks_error: str | None,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "branch": branch,
        "base_ref": report.base.ref,
        "remote_checked": report.base.remote_checked,
        "base_reason": report.base.reason,
        "behind_count": report.behind_count,
        "has_conflicts": report.has_conflicts,
        "conflict_probe": report.conflict_probe,
        "reason": report.reason,
        "checks": checks,
        "checks_error": checks_error,
    }


def merge_check(
    task_id: str = typer.Argument(
        ..., help="Task whose claim branch to check against the base."
    ),
    run_checks: bool = typer.Option(  # noqa: B008
        False,
        "--run-checks",
        help=(
            "Also build a throwaway worktree of the merged tree and run the "
            "task's verification commands inside it."
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
    """Pre-merge gate: base freshness + optional merged-tree verification.

    Reports how far TASK_ID's claim branch sits behind the integration base
    (``origin/<default>`` when reachable, the local default branch offline)
    and whether it textually conflicts. With ``--run-checks``, additionally
    runs the task's verification commands against the would-be merge result
    in a throwaway worktree — the user's working tree and current branch are
    never touched.

    Exit 0 when fresh (or offline-degraded); exit 1 when verifiably stale,
    conflicted, or a merged-tree check fails.
    """
    from anvil.cli._helpers import StateRootError, _open_backend

    project_root = (cwd or Path.cwd()).resolve()

    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError as exc:
        if json_output:
            fail(_COMMAND, str(exc), code="state_root_invalid")
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if not state_dir.exists():
        if json_output:
            fail(_COMMAND, "anvil not initialized in this project.", code="not_initialized")
        typer.echo("anvil not initialized in this project. Run `anvil init` to start.")
        raise typer.Exit(code=1)

    backend = _open_backend(state_dir)
    try:
        task = backend.get_task(task_id)
        if task is None:
            if json_output:
                fail(_COMMAND, f"task '{task_id}' not found.", code="task_not_found")
            typer.echo(f"Error: task '{task_id}' not found.", err=True)
            raise typer.Exit(code=1)

        branch = _branch_for_task(backend, task_id, project_root)
        if branch is None:
            message = (
                f"no claim with a recorded branch found for '{task_id}' — "
                "claim the task first (or the project has no git repo)."
            )
            if json_output:
                fail(_COMMAND, message, code="branch_not_found")
            typer.echo(f"Error: {message}", err=True)
            raise typer.Exit(code=1)

        verification_commands = list(task.verification.commands)
    finally:
        backend.close()

    base = resolve_base(project_root)
    report = check_freshness(branch, cwd=project_root, base=base)

    checks: list[dict[str, object]] = []
    checks_error: str | None = None
    if run_checks:
        if report.has_conflicts:
            checks_error = "merged tree conflicts; checks skipped"
        elif report.base.ref is None:
            checks_error = f"no base ref ({report.base.reason}); checks skipped"
        elif not verification_commands:
            checks_error = "task declares no verification commands"
        else:
            commit_oid, build_error = _merged_tree_commit(
                report.base.ref, branch, project_root
            )
            if commit_oid is None:
                checks_error = build_error
            else:
                worktree_path = (
                    state_dir
                    / "tmp"
                    / f"merge-check-{safe_path_component(task_id).lower()}"
                )
                checks, checks_error = _run_merged_tree_checks(
                    verification_commands,
                    commit_oid,
                    repo_root=project_root,
                    worktree_path=worktree_path,
                )

    failed_checks = [
        c for c in checks if c["exit_code"] != 0 or c["exit_code"] is None
    ]
    stale = bool(report.behind_count)
    conflicted = report.has_conflicts is True
    ok = not stale and not conflicted and not failed_checks

    if json_output:
        data = _json_data(task_id, branch, report, checks, checks_error)
        data["ok"] = ok
        if ok:
            emit_success(_COMMAND, data)
        else:
            parts = []
            if stale:
                parts.append(f"branch is {report.behind_count} commit(s) behind {report.base.ref}")
            if conflicted:
                parts.append("merged tree has textual conflicts")
            if failed_checks:
                parts.append(f"{len(failed_checks)} merged-tree check(s) failed")
            fail(_COMMAND, "; ".join(parts), code="merge_check_failed")
        return

    typer.echo(f"merge-check {task_id} (branch: {branch})")
    base_label = report.base.ref or "(unresolved)"
    origin_note = "" if report.base.remote_checked else f"  [local base: {report.base.reason}]"
    typer.echo(f"  Base:      {base_label}{origin_note}")
    behind_label = (
        str(report.behind_count)
        if report.behind_count is not None
        else f"unknown ({report.reason})"
    )
    typer.echo(f"  Behind:    {behind_label}")
    conflict_label = (
        "yes"
        if report.has_conflicts
        else ("no" if report.has_conflicts is False else report.conflict_probe)
    )
    typer.echo(f"  Conflicts: {conflict_label}")
    if run_checks:
        if checks_error:
            typer.echo(f"  Checks:    {checks_error}")
        for check in checks:
            code = check["exit_code"]
            status = "PASS" if code == 0 else f"FAIL ({check['detail'] or f'exit {code}'})"
            typer.echo(f"  Check:     {status}  $ {check['command']}")

    if ok:
        typer.echo("Result: OK — branch is fresh against its base.")
        return
    if stale:
        typer.echo(
            f"Result: STALE — {report.behind_count} commit(s) behind "
            f"{report.base.ref}. Rebase/merge the base and re-verify.",
            err=True,
        )
    if conflicted:
        typer.echo("Result: CONFLICTS — the merge would not apply cleanly.", err=True)
    if failed_checks:
        for check in failed_checks:
            typer.echo(
                f"Result: CHECK FAILED — $ {check['command']}", err=True
            )
    raise typer.Exit(code=1)
