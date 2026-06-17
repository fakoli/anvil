"""Tests for fakoli_state.git_ops.branch and fakoli_state.git_ops.worktree.

Uses real git (tmp git init per test) — no mocking.

Coverage target: git_ops/ >= 85%.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fakoli_state.git_ops.branch import (
    BranchResult,
    _slug,
    create_branch_for_task,
    is_git_available,
    is_git_repo,
)
from fakoli_state.git_ops.worktree import (
    WorktreeResult,
    create_worktree_for_task,
)

# ---------------------------------------------------------------------------
# Git repo fixture
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> Path:
    """Initialise a git repo in *path* with one initial commit so HEAD exists."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.test"],
        cwd=str(path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(path), check=True, capture_output=True,
    )
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(path), check=True, capture_output=True,
    )
    return path


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real git repository with one initial commit."""
    return _init_git_repo(tmp_path / "repo")


# ---------------------------------------------------------------------------
# TestIsGitAvailable
# ---------------------------------------------------------------------------


class TestIsGitAvailable:
    def test_is_git_available_returns_true_when_git_on_path(self) -> None:
        """is_git_available() returns True on CI where git is installed."""
        # This verifies the function doesn't crash and returns a bool.
        result = is_git_available()
        assert isinstance(result, bool)
        # On any CI or dev machine where this test suite runs, git must be present.
        assert result is True


# ---------------------------------------------------------------------------
# TestIsGitRepo
# ---------------------------------------------------------------------------


class TestIsGitRepo:
    def test_is_git_repo_true_in_repo(self, git_repo: Path) -> None:
        """is_git_repo returns True inside a git repository."""
        assert is_git_repo(git_repo) is True

    def test_is_git_repo_false_outside_repo(self, tmp_path: Path) -> None:
        """is_git_repo returns False in a directory that is NOT a git repo."""
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        assert is_git_repo(non_repo) is False


# ---------------------------------------------------------------------------
# TestSlug (internal helper — tested for coverage)
# ---------------------------------------------------------------------------


class TestSlug:
    def test_slug_lowercases(self) -> None:
        assert _slug("Hello World") == "hello-world"

    def test_slug_replaces_specials(self) -> None:
        result = _slug("Add retry: now!")
        assert result.isalnum() or "-" in result
        assert result == result.lower()

    def test_slug_truncates(self) -> None:
        long_title = "a" * 100
        assert len(_slug(long_title)) <= 40

    def test_slug_collapses_repeated_hyphens(self) -> None:
        result = _slug("a  b  c")
        assert "--" not in result

    def test_slug_falls_back_to_task_for_empty(self) -> None:
        # A title that produces no alphanumeric chars
        assert _slug("!!!") == "task"


# ---------------------------------------------------------------------------
# TestCreateBranchForTask
# ---------------------------------------------------------------------------


class TestCreateBranchForTask:
    def test_create_branch_happy_path(self, git_repo: Path) -> None:
        """task T001 + title 'Add retry' → branch 'agent/t001-add-retry'; created=True."""
        result = create_branch_for_task("T001", "Add retry", cwd=git_repo)
        assert isinstance(result, BranchResult)
        assert result.created is True
        assert result.branch is not None
        assert result.branch.startswith("agent/t001-")
        assert "retry" in result.branch

    def test_create_branch_slug_lowercase_alphanumeric(self, git_repo: Path) -> None:
        """Title with special chars produces a clean lowercase slug."""
        result = create_branch_for_task("T002", "Feat: Auth Tokens!", cwd=git_repo)
        assert result.created is True
        assert result.branch is not None
        # Branch name must be lowercase and contain no special chars except - and /
        branch_part = result.branch.split("agent/")[1]
        for ch in branch_part:
            assert ch.isalnum() or ch in ("-", "/"), f"Invalid char {ch!r} in branch {result.branch!r}"

    def test_create_branch_truncates_long_titles(self, git_repo: Path) -> None:
        """A 200-char title produces a branch name <= 80 chars total."""
        long_title = "x" * 200
        result = create_branch_for_task("T003", long_title, cwd=git_repo)
        assert result.created is True
        assert result.branch is not None
        assert len(result.branch) <= 80

    def test_create_branch_handles_name_collision(self, git_repo: Path) -> None:
        """Creating the same branch twice produces a -2 suffix the second time."""
        result1 = create_branch_for_task("T004", "Add retry", cwd=git_repo)
        assert result1.created is True
        base_branch = result1.branch

        # Checkout a different branch so we can re-create the original name
        subprocess.run(
            ["git", "checkout", "-b", "temp-branch"],
            cwd=str(git_repo), check=True, capture_output=True,
        )

        result2 = create_branch_for_task("T004", "Add retry", cwd=git_repo)
        assert result2.created is True
        assert result2.branch != base_branch
        assert result2.reason == "renamed due to collision"
        # Collision suffix appended
        assert result2.branch is not None and (
            result2.branch.endswith("-2") or "-2" in result2.branch
        )

    def test_create_branch_returns_failure_outside_git_repo(self, tmp_path: Path) -> None:
        """create_branch_for_task returns created=False outside a git repo."""
        non_repo = tmp_path / "no-git"
        non_repo.mkdir()
        result = create_branch_for_task("T005", "Some title", cwd=non_repo)
        assert result.created is False
        assert result.branch is None
        assert result.reason is not None

    def test_create_branch_actually_checks_out_branch(self, git_repo: Path) -> None:
        """After create_branch_for_task, 'git branch --show-current' returns the new branch."""
        result = create_branch_for_task("T006", "Implement auth", cwd=git_repo)
        assert result.created is True

        current = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(git_repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert current == result.branch

    def test_custom_branch_prefix_feature(self, git_repo: Path) -> None:
        """v1.15.0: host projects that use the `feature/` convention can
        set `branch_prefix: "feature"` in config.yaml; claim creates
        `feature/<task>-<slug>` instead of `agent/<task>-<slug>`."""
        result = create_branch_for_task(
            "T010", "Add caching", cwd=git_repo, branch_prefix="feature"
        )
        assert result.created is True
        assert result.branch is not None
        assert result.branch.startswith("feature/t010-")
        assert "agent" not in result.branch

    def test_custom_branch_prefix_fix(self, git_repo: Path) -> None:
        result = create_branch_for_task(
            "T011", "Repair leak", cwd=git_repo, branch_prefix="fix"
        )
        assert result.created is True
        assert result.branch is not None
        assert result.branch.startswith("fix/t011-")

    def test_nested_branch_prefix_allowed(self, git_repo: Path) -> None:
        """`feature/agent` — host project's prefix + the agent marker. Both
        signals preserved."""
        result = create_branch_for_task(
            "T012", "Do thing", cwd=git_repo, branch_prefix="feature/agent"
        )
        assert result.created is True
        assert result.branch is not None
        assert result.branch.startswith("feature/agent/t012-")

    def test_empty_branch_prefix_omits_separator(self, git_repo: Path) -> None:
        """`branch_prefix: ""` is the explicit no-prefix mode — branch is
        just `<task>-<slug>` with no leading prefix or slash."""
        result = create_branch_for_task(
            "T013", "Bare branch", cwd=git_repo, branch_prefix=""
        )
        assert result.created is True
        assert result.branch is not None
        assert result.branch == "t013-bare-branch"
        assert "/" not in result.branch

    def test_default_prefix_is_agent_for_backwards_compat(self, git_repo: Path) -> None:
        """Pre-v1.15.0 callers that don't pass branch_prefix get the
        original `agent/` default."""
        result = create_branch_for_task("T014", "Default behaviour", cwd=git_repo)
        assert result.created is True
        assert result.branch is not None
        assert result.branch.startswith("agent/t014-")


# ---------------------------------------------------------------------------
# TestCreateWorktreeForTask
# ---------------------------------------------------------------------------


class TestCreateWorktreeForTask:
    def test_create_worktree_happy_path(self, tmp_path: Path) -> None:
        """A branch must exist before creating a worktree. Create branch then worktree."""
        repo = _init_git_repo(tmp_path / "repo")
        # Create branch first
        branch_result = create_branch_for_task("T007", "Add feature", cwd=repo)
        assert branch_result.created is True
        assert branch_result.branch is not None

        # Go back to main/master so we can add a worktree on the branch
        subprocess.run(
            ["git", "checkout", "master"],
            cwd=str(repo), capture_output=True,
        )
        # If 'master' doesn't work, try 'main'
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(repo), capture_output=True,
        )

        wt_dir = tmp_path / "worktrees"
        wt_dir.mkdir()
        result = create_worktree_for_task(
            "T007", branch_result.branch, cwd=repo, parent_dir=wt_dir / "wt-t007"
        )
        assert isinstance(result, WorktreeResult)
        assert result.created is True
        assert result.path is not None
        assert "wt-t007" in result.path

    def test_create_worktree_refuses_dirty_tree(self, tmp_path: Path) -> None:
        """Dirty working tree (uncommitted changes) prevents worktree creation."""
        repo = _init_git_repo(tmp_path / "repo")
        # Create a branch so we have something to attach a worktree to
        branch_result = create_branch_for_task("T008", "Dirty test", cwd=repo)
        assert branch_result.created is True

        # Check out main/master branch and dirty it
        subprocess.run(["git", "checkout", "master"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True)

        # Create an unstaged change
        (repo / "dirty_file.txt").write_text("uncommitted change\n", encoding="utf-8")

        result = create_worktree_for_task(
            "T008", branch_result.branch or "agent/t008-dirty-test", cwd=repo
        )
        assert result.created is False
        assert result.reason is not None
        assert "dirty" in result.reason.lower() or "worktree" in result.reason.lower()

    def test_create_worktree_returns_failure_outside_git_repo(self, tmp_path: Path) -> None:
        """create_worktree_for_task returns created=False outside a git repo."""
        non_repo = tmp_path / "no-git"
        non_repo.mkdir()
        result = create_worktree_for_task("T009", "some-branch", cwd=non_repo)
        assert result.created is False
        assert result.branch if hasattr(result, "branch") else True  # no branch attr
        assert result.reason is not None
