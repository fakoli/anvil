"""Tests for anvil.git_ops.branch and anvil.git_ops.worktree.

Uses real git (tmp git init per test) — no mocking.

Coverage target: git_ops/ >= 85%.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from anvil.git_ops import freshness as freshness_mod
from anvil.git_ops import worktree as worktree_mod
from anvil.git_ops.branch import (
    BranchResult,
    _slug,
    branch_name_for_task,
    create_branch_for_task,
    is_git_available,
    is_git_repo,
    use_named_branch,
)
from anvil.git_ops.freshness import BaseRef, check_freshness, resolve_base
from anvil.git_ops.worktree import (
    ClaimPlanError,
    WorktreeResult,
    apply_claim_plan,
    canonical_git_root,
    claim_git_metadata,
    compensate_claim_plan,
    create_worktree_for_task,
    resolve_claim_plan,
    revalidate_claim_plan,
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
    @pytest.mark.slow
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


@pytest.mark.slow
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

    def test_create_branch_sanitizes_namespaced_task_id(self, git_repo: Path) -> None:
        """#108.1: a namespaced task id (``prd:T001``) yields a colon-free, valid
        git refname — ``:`` is illegal in a refname."""
        result = create_branch_for_task(
            "advise-and-defer:T005", "Live validate failover", cwd=git_repo
        )
        assert result.created is True
        assert result.branch is not None
        assert ":" not in result.branch
        check = subprocess.run(
            ["git", "check-ref-format", "--branch", result.branch],
            cwd=str(git_repo), capture_output=True, text=True,
        )
        assert check.returncode == 0, check.stderr

    def test_create_branch_without_checkout_leaves_head_in_place(
        self, git_repo: Path
    ) -> None:
        """#104: ``checkout=False`` creates the ref but does NOT move the current
        worktree onto it, so ``claim --worktree`` can hand the branch to
        ``git worktree add`` (a branch checked out in main can't be added
        elsewhere)."""
        before = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(git_repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        result = create_branch_for_task(
            "T020", "No checkout", cwd=git_repo, checkout=False
        )
        assert result.created is True
        assert result.branch is not None
        after = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(git_repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert after == before  # HEAD did not move
        listed = subprocess.run(
            ["git", "branch", "--list", result.branch],
            cwd=str(git_repo), capture_output=True, text=True, check=True,
        ).stdout
        assert result.branch in listed  # but the branch was created

    def test_use_named_branch_no_checkout_new_branch(self, git_repo: Path) -> None:
        """#104: use_named_branch(checkout=False) creates a NEW named branch
        without moving main's HEAD, so --branch + --worktree works."""
        before = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(git_repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        result = use_named_branch("my-feature", cwd=git_repo, checkout=False)
        assert result.created is True
        assert result.branch == "my-feature"
        after = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(git_repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert after == before
        listed = subprocess.run(
            ["git", "branch", "--list", "my-feature"],
            cwd=str(git_repo), capture_output=True, text=True, check=True,
        ).stdout
        assert "my-feature" in listed

    def test_use_named_branch_no_checkout_existing_branch(self, git_repo: Path) -> None:
        """#104: for an EXISTING branch, use_named_branch(checkout=False) leaves
        HEAD in place (so git worktree add can check it out elsewhere)."""
        use_named_branch("existing-x", cwd=git_repo, checkout=False)  # create it
        before = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(git_repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        result = use_named_branch("existing-x", cwd=git_repo, checkout=False)
        assert result.created is True
        assert result.branch == "existing-x"
        after = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(git_repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert after == before

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


@pytest.mark.slow
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

    def test_create_worktree_sanitizes_namespaced_task_id(self, tmp_path: Path) -> None:
        """#105: the worktree directory name for a namespaced id has no ``:``
        (an NTFS alternate-data-stream separator / invalid Windows path char)."""
        repo = _init_git_repo(tmp_path / "repo")
        # checkout=False so the branch isn't held by the main worktree.
        br = create_branch_for_task(
            "advise-and-defer:T005", "live validate", cwd=repo, checkout=False
        )
        assert br.created and br.branch
        result = create_worktree_for_task("advise-and-defer:T005", br.branch, cwd=repo)
        assert result.created is True, result.reason
        assert result.path is not None
        assert ":" not in Path(result.path).name
        assert Path(result.path).name.lower() == "wt-advise-and-defer-t005"

    def test_create_worktree_returns_failure_outside_git_repo(self, tmp_path: Path) -> None:
        """create_worktree_for_task returns created=False outside a git repo."""
        non_repo = tmp_path / "no-git"
        non_repo.mkdir()
        result = create_worktree_for_task("T009", "some-branch", cwd=non_repo)
        assert result.created is False
        assert result.branch if hasattr(result, "branch") else True  # no branch attr
        assert result.reason is not None


# ---------------------------------------------------------------------------
# Workspace-layout regression: git ops must target the project, not the
# HOME workspace (found 2026-07-02 reproducing the README flow on 0.3.0 —
# every workspace-layout claim printed "git branch not created" because
# claim resolved its git cwd through the state base dir).
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestWorkspaceLayoutGitOps:
    """`anvil claim` in the default HOME-workspace layout: state lands in
    ~/.anvil/workspaces/<key>/, but the agent/<task>-<slug> branch must be
    created in the user's actual project repository."""

    def test_claim_creates_branch_in_project_repo_under_workspace_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from anvil.cli import app

        project = _init_git_repo(tmp_path / "proj")
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        # Path.home() reads USERPROFILE on Windows, HOME on POSIX — set both so
        # the HOME-workspace redirect actually isolates the test cross-platform.
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
        monkeypatch.delenv("ANVIL_ROOT", raising=False)
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(app, ["init", "--with-sample"])
        assert result.exit_code == 0, result.output
        # Sanity: workspace layout is active — state in HOME, not ./.anvil.
        assert not (project / ".anvil").exists()
        assert (home / ".anvil" / "workspaces").exists()

        result = runner.invoke(app, ["claim", "T001"])
        assert result.exit_code == 0, result.output
        assert "git branch not created" not in result.output

        branches = subprocess.run(
            ["git", "branch", "--list"],
            cwd=str(project),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "agent/t001" in branches, branches

    def test_claim_json_returns_branch_with_no_warnings_under_workspace_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#104 regression (--json path): ``claim --json`` in the default
        HOME-workspace layout returns a NON-NULL ``branch`` with empty
        ``warnings`` — git ops resolve the project repo independently of the
        (non-repo) state/workspace dir, so branch creation is no longer a silent
        no-op. The pre-existing test above only asserts the human-output text.

        NOTE: this deliberately does NOT pass ``--worktree`` — the worktree path
        is still broken (``claim`` checks the branch out in the MAIN repo, so
        ``git worktree add`` then fails 'already used by worktree'), tracked as a
        separate follow-up. This test locks the branch half that #104's fix
        actually delivered."""
        import json as _json

        from typer.testing import CliRunner

        from anvil.cli import app

        project = _init_git_repo(tmp_path / "proj")
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        # Path.home() reads USERPROFILE on Windows, HOME on POSIX — set both so
        # the HOME-workspace redirect actually isolates the test cross-platform.
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
        monkeypatch.delenv("ANVIL_ROOT", raising=False)
        monkeypatch.chdir(project)

        runner = CliRunner()
        assert runner.invoke(app, ["init", "--with-sample"]).exit_code == 0

        result = runner.invoke(app, ["claim", "T001", "--json"])
        assert result.exit_code == 0, result.output
        data = _json.loads(result.stdout)["data"]
        assert data["branch"], data
        assert data["warnings"] == [], data

    def test_claim_worktree_json_creates_worktree_and_leaves_main_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#104 (worktree half): ``claim --worktree --json`` in the default
        HOME-workspace layout creates a REAL worktree (non-null, empty warnings)
        and leaves the MAIN repo on its ORIGINAL branch — the agent branch is
        checked out only in the new worktree. Before the fix, claim checked the
        branch out in main, so ``git worktree add`` failed 'already used'."""
        import json as _json

        from typer.testing import CliRunner

        from anvil.cli import app

        project = _init_git_repo(tmp_path / "proj")
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
        monkeypatch.delenv("ANVIL_ROOT", raising=False)
        monkeypatch.chdir(project)

        before = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(project), capture_output=True, text=True, check=True,
        ).stdout.strip()

        runner = CliRunner()
        assert runner.invoke(app, ["init", "--with-sample"]).exit_code == 0

        result = runner.invoke(app, ["claim", "T001", "--worktree", "--json"])
        assert result.exit_code == 0, result.output
        data = _json.loads(result.stdout)["data"]
        assert data["branch"], data
        assert data["worktree"], data
        assert data["warnings"] == [], data
        git_metadata = data["claim"]["git_metadata"]
        assert git_metadata["mode"] == "isolated"
        assert git_metadata["branch"] == data["branch"]
        assert git_metadata["worktree_path"] == data["worktree"]
        assert git_metadata["claim_start_sha"] == subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert data["claim"]["attestation_context"]["claim_start_sha"] == (
            git_metadata["claim_start_sha"]
        )
        from anvil.cli._helpers import _open_backend, _resolve_state_dir

        state_dir = _resolve_state_dir(project)
        persisted_backend = _open_backend(state_dir)
        try:
            persisted = persisted_backend.get_claim(data["claim"]["id"])
            assert persisted is not None
            assert persisted.git_metadata is not None
            assert persisted.git_metadata.model_dump(mode="json") == git_metadata
        finally:
            persisted_backend.close()
        from anvil.clock import SystemClock
        from anvil.state.sqlite import SqliteBackend

        replay_dir = tmp_path / "replay"
        replay_dir.mkdir()
        replay = SqliteBackend(
            db_path=str(replay_dir / "state.db"),
            events_path=str(replay_dir / "events.jsonl"),
            clock=SystemClock(),
        )
        replay.initialize()
        try:
            replay.replay_from_empty(str(state_dir / "events.jsonl"))
            replayed = replay.get_claim(data["claim"]["id"])
            assert replayed is not None
            assert replayed.git_metadata is not None
            assert replayed.git_metadata.model_dump(mode="json") == git_metadata
        finally:
            replay.close()
        assert Path(data["worktree"]).exists()

        after = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert after == before, "main checkout must stay on its original branch"

    def test_claim_named_branch_worktree_leaves_main_and_creates_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#104: --branch + --worktree combined creates the worktree and leaves
        main's HEAD in place. (The T002 review found this combo still checked the
        named branch out in main and failed 'already used by worktree'.)"""
        import json as _json

        from typer.testing import CliRunner

        from anvil.cli import app

        project = _init_git_repo(tmp_path / "proj")
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
        monkeypatch.delenv("ANVIL_ROOT", raising=False)
        monkeypatch.chdir(project)

        before = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(project), capture_output=True, text=True, check=True,
        ).stdout.strip()

        runner = CliRunner()
        assert runner.invoke(app, ["init", "--with-sample"]).exit_code == 0

        result = runner.invoke(
            app, ["claim", "T001", "--branch", "feat-x", "--worktree", "--json"]
        )
        assert result.exit_code == 0, result.output
        data = _json.loads(result.stdout)["data"]
        assert data["worktree"], data
        assert data["warnings"] == [], data
        assert Path(data["worktree"]).exists()

        after = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(project), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert after == before, "main must not move onto the named branch"

    def test_claim_git_failure_releases_state_and_leaves_no_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json as _json

        from typer.testing import CliRunner

        import anvil.git_ops as git_ops
        from anvil.cli import app

        project = _init_git_repo(tmp_path / "proj")
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
        monkeypatch.delenv("ANVIL_ROOT", raising=False)
        monkeypatch.chdir(project)
        runner = CliRunner()
        assert runner.invoke(app, ["init", "--with-sample"]).exit_code == 0
        plan = resolve_claim_plan("T001", "Set up project structure", cwd=project)
        original_ref = _default_branch(project)
        original_sha = _git(project, "rev-parse", "HEAD")

        def refuse(_plan, *, cwd=None):  # type: ignore[no-untyped-def]
            raise ClaimPlanError("injected_git_failure", "injected Git failure")

        monkeypatch.setattr(git_ops, "apply_claim_plan", refuse)
        result = runner.invoke(app, ["claim", "T001", "--json"])

        assert result.exit_code == 1
        error = _json.loads(result.stdout)["error"]
        assert error["code"] == "injected_git_failure"
        listing = runner.invoke(app, ["list", "--json"])
        assert listing.exit_code == 0, listing.output
        tasks = _json.loads(listing.stdout)["data"]["tasks"]
        assert next(task for task in tasks if task["id"] == "T001")["status"] == "ready"
        assert not _ref_exists(project, f"refs/heads/{plan.branch}")
        assert _default_branch(project) == original_ref
        assert _git(project, "rev-parse", "HEAD") == original_sha

    def test_bundle_claim_worktree_persists_same_git_binding_on_members(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json as _json

        from typer.testing import CliRunner

        from anvil.cli import app
        from anvil.cli._helpers import _open_backend, _resolve_state_dir

        project = _init_git_repo(tmp_path / "proj")
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
        monkeypatch.delenv("ANVIL_ROOT", raising=False)
        monkeypatch.chdir(project)
        runner = CliRunner()
        assert runner.invoke(app, ["init", "--with-sample"]).exit_code == 0
        created = runner.invoke(
            app,
            ["bundle", "create", "B001", "T001", "--prd", "default", "--json"],
        )
        assert created.exit_code == 0, created.output

        result = runner.invoke(
            app, ["claim", "B001", "--bundle", "--worktree", "--json"]
        )

        assert result.exit_code == 0, result.output
        data = _json.loads(result.stdout)["data"]
        metadata = data["claim"]["git_metadata"]
        assert metadata["mode"] == "isolated"
        assert metadata["branch"] == data["branch"]
        assert metadata["worktree_path"] == data["worktree"]
        assert _default_branch(project) != data["branch"]
        backend = _open_backend(_resolve_state_dir(project))
        try:
            bundle_claim = backend.get_bundle_claim("B001")
            assert bundle_claim is not None
            assert bundle_claim.git_metadata is not None
            assert bundle_claim.git_metadata.model_dump(mode="json") == metadata
            member = backend.get_claim(bundle_claim.member_claim_ids["T001"])
            assert member is not None
            assert member.git_metadata is not None
            assert member.git_metadata.model_dump(mode="json") == metadata
        finally:
            backend.close()

    def test_bundle_git_failure_releases_state_and_owned_ref(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json as _json

        from typer.testing import CliRunner

        import anvil.git_ops as git_ops
        from anvil.cli import app
        from anvil.cli._helpers import _open_backend, _resolve_state_dir

        project = _init_git_repo(tmp_path / "proj")
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
        monkeypatch.delenv("ANVIL_ROOT", raising=False)
        monkeypatch.chdir(project)
        runner = CliRunner()
        assert runner.invoke(app, ["init", "--with-sample"]).exit_code == 0
        created = runner.invoke(
            app,
            ["bundle", "create", "B001", "T001", "--prd", "default", "--json"],
        )
        assert created.exit_code == 0, created.output
        plan = resolve_claim_plan("B001", "Bundle B001", cwd=project)

        def refuse(_plan, *, cwd=None):  # type: ignore[no-untyped-def]
            raise ClaimPlanError("injected_git_failure", "injected Git failure")

        monkeypatch.setattr(git_ops, "apply_claim_plan", refuse)
        result = runner.invoke(app, ["claim", "B001", "--bundle", "--json"])

        assert result.exit_code == 1
        assert _json.loads(result.stdout)["error"]["code"] == "injected_git_failure"
        backend = _open_backend(_resolve_state_dir(project))
        try:
            bundle = backend.get_bundle("B001")
            assert bundle is not None and bundle.status == "replan_required"
            bundle_claim = backend.get_bundle_claim("B001")
            assert bundle_claim is not None
            assert bundle_claim.status.value == "released"
            assert bundle_claim.release_reason == "transactional Git claim failed"
            task = backend.get_task("T001")
            assert task is not None and task.status.value == "ready"
        finally:
            backend.close()
        assert not _ref_exists(project, f"refs/heads/{plan.branch}")


# ---------------------------------------------------------------------------
# TestFreshness (retro-opps:T005) — base resolution + freshness/conflict report
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _ref_exists(repo: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", ref],
        cwd=str(repo),
        check=False,
    ).returncode == 0


def _default_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _configure_tracking(repo: Path, branch: str, remote_sha: str) -> str:
    if not _git(repo, "remote"):
        _git(repo, "remote", "add", "origin", "https://example.invalid/repo.git")
    _git(repo, "config", f"branch.{branch}.remote", "origin")
    _git(repo, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
    remote_ref = f"refs/remotes/origin/{branch}"
    _git(repo, "update-ref", remote_ref, remote_sha)
    return remote_ref


@pytest.mark.slow
class TestResolveClaimPlan:
    def test_claim_plan_state_only_and_isolation_refusal(self, tmp_path: Path) -> None:
        non_repo = tmp_path / "not-git"
        non_repo.mkdir()

        plan = resolve_claim_plan("T001", "State only", cwd=non_repo)

        assert plan.mode == "state_only"
        assert plan.git_metadata_available is False
        assert plan.branch is None
        assert plan.claim_start_sha is None
        assert plan.revalidation_preconditions == ()
        assert "state-only" in plan.warnings[0]
        with pytest.raises(ClaimPlanError, match="isolated claim") as exc:
            resolve_claim_plan("T001", "Needs Git", cwd=non_repo, worktree=True)
        assert exc.value.code == "git_required"

    def test_claim_plan_dirty_isolated_is_read_only(self, git_repo: Path) -> None:
        dirty = git_repo / "untracked.txt"
        dirty.write_text("preserve me\n", encoding="utf-8")
        before = {
            "head": _git(git_repo, "rev-parse", "HEAD"),
            "branch": _default_branch(git_repo),
            "refs": _git(git_repo, "for-each-ref", "--format=%(refname) %(objectname)"),
            "status": _git(git_repo, "status", "--porcelain=v1", "--untracked-files=all"),
            "worktrees": _git(git_repo, "worktree", "list", "--porcelain"),
        }
        git_dir = Path(_git(git_repo, "rev-parse", "--absolute-git-dir"))
        git_artifacts_before = {
            str(path.relative_to(git_dir)): path.read_bytes()
            for path in git_dir.rglob("*")
            if path.is_file()
        }

        plan = resolve_claim_plan(
            "prd:T001",
            "Read only planning",
            cwd=git_repo,
            worktree=True,
        )

        assert plan.mode == "isolated"
        assert plan.caller_dirty is True
        assert plan.claim_start_sha == plan.selected_default_base_sha
        assert plan.branch == "agent/prd-t001-read-only-planning"
        assert plan.branch_exists is False
        assert plan.branch_owner_path is None
        assert plan.target_owner_branch is None
        assert plan.worktrees[0].path == str(git_repo.resolve())
        assert {item.kind for item in plan.revalidation_preconditions} >= {
            "caller_head",
            "ref_oid",
            "topology",
            "path",
        }
        assert any("never moves" in warning for warning in plan.warnings)
        assert dirty.read_text(encoding="utf-8") == "preserve me\n"
        assert not (git_repo / ".anvil").exists()
        after = {
            "head": _git(git_repo, "rev-parse", "HEAD"),
            "branch": _default_branch(git_repo),
            "refs": _git(git_repo, "for-each-ref", "--format=%(refname) %(objectname)"),
            "status": _git(git_repo, "status", "--porcelain=v1", "--untracked-files=all"),
            "worktrees": _git(git_repo, "worktree", "list", "--porcelain"),
        }
        assert after == before
        assert {
            str(path.relative_to(git_dir)): path.read_bytes()
            for path in git_dir.rglob("*")
            if path.is_file()
        } == git_artifacts_before

    def test_claim_plan_dirty_shared_refuses(self, git_repo: Path) -> None:
        (git_repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        with pytest.raises(ClaimPlanError, match="clean caller") as exc:
            resolve_claim_plan("T002", "Shared", cwd=git_repo)

        assert exc.value.code == "dirty_shared_tree"

    @pytest.mark.parametrize("ahead", ["local", "remote"])
    def test_worktree_base_selects_known_descendant(
        self,
        git_repo: Path,
        ahead: str,
    ) -> None:
        default = _default_branch(git_repo)
        common_sha = _git(git_repo, "rev-parse", "HEAD")
        remote_ref = _configure_tracking(git_repo, default, common_sha)
        if ahead == "local":
            expected = _commit_file(
                git_repo,
                "local.txt",
                "local\n",
                "local ahead",
            )
            expected_ref = f"refs/heads/{default}"
        else:
            _git(git_repo, "checkout", "-b", "remote-tip")
            expected = _commit_file(
                git_repo,
                "remote.txt",
                "remote\n",
                "remote ahead",
            )
            _git(git_repo, "checkout", default)
            _git(git_repo, "update-ref", remote_ref, expected)
            expected_ref = remote_ref

        plan = resolve_claim_plan("T003", "Descendant", cwd=git_repo, worktree=True)

        assert plan.selected_default_base_ref == expected_ref
        assert plan.selected_default_base_sha == expected
        assert plan.claim_start_sha == expected
        assert any("without" in warning or "not fetched" in warning for warning in plan.warnings)

    def test_worktree_base_divergence_refuses_without_fetch(
        self,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        default = _default_branch(git_repo)
        common_sha = _git(git_repo, "rev-parse", "HEAD")
        remote_ref = _configure_tracking(git_repo, default, common_sha)
        _git(git_repo, "checkout", "-b", "remote-tip")
        remote_sha = _commit_file(git_repo, "remote.txt", "r\n", "remote")
        _git(git_repo, "checkout", default)
        _commit_file(git_repo, "local.txt", "l\n", "local")
        _git(git_repo, "update-ref", remote_ref, remote_sha)
        real_run_git = worktree_mod._run_git

        def refuse_fetch(args: list[str], cwd: Path):  # type: ignore[no-untyped-def]
            assert "fetch" not in args
            return real_run_git(args, cwd)

        monkeypatch.setattr(worktree_mod, "_run_git", refuse_fetch)
        with pytest.raises(ClaimPlanError, match="diverged") as exc:
            resolve_claim_plan("T004", "Diverged", cwd=git_repo, worktree=True)
        assert exc.value.code == "default_refs_diverged"
        assert not (git_repo / ".git" / "FETCH_HEAD").exists()

    def test_worktree_base_offline_upstream_warns(self, git_repo: Path) -> None:
        default = _default_branch(git_repo)
        local_sha = _git(git_repo, "rev-parse", "HEAD")
        _configure_tracking(git_repo, default, local_sha)
        _git(git_repo, "update-ref", "-d", f"refs/remotes/origin/{default}")

        plan = resolve_claim_plan("T005", "Offline", cwd=git_repo, worktree=True)

        assert plan.selected_default_base_ref == f"refs/heads/{default}"
        assert plan.selected_default_base_sha == local_sha
        assert plan.upstream_default_ref == f"refs/remotes/origin/{default}"
        assert plan.upstream_default_sha is None
        assert any("unavailable locally" in warning for warning in plan.warnings)

    def test_worktree_base_remote_head_name_precedes_main_master(
        self,
        git_repo: Path,
    ) -> None:
        head_sha = _git(git_repo, "rev-parse", "HEAD")
        _git(git_repo, "branch", "trunk")
        remote_ref = _configure_tracking(git_repo, "trunk", head_sha)
        _git(
            git_repo,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            remote_ref,
        )

        plan = resolve_claim_plan("T005A", "Precedence", cwd=git_repo, worktree=True)

        assert plan.default_discovery == "remote HEAD name"
        assert plan.local_default_ref == "refs/heads/trunk"
        assert plan.upstream_default_ref == remote_ref
        assert plan.selected_default_base_sha == head_sha

    def test_worktree_base_unverifiable_ancestry_warns_and_uses_local(
        self,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        default = _default_branch(git_repo)
        local_sha = _git(git_repo, "rev-parse", "HEAD")
        remote_ref = _configure_tracking(git_repo, default, local_sha)
        _git(git_repo, "checkout", "-b", "remote-tip")
        remote_sha = _commit_file(git_repo, "remote.txt", "r\n", "remote")
        _git(git_repo, "checkout", default)
        _git(git_repo, "update-ref", remote_ref, remote_sha)
        monkeypatch.setattr(worktree_mod, "_is_ancestor", lambda *_args: None)

        plan = resolve_claim_plan(
            "T005B",
            "Unverifiable",
            cwd=git_repo,
            worktree=True,
        )

        assert plan.selected_default_base_ref == f"refs/heads/{default}"
        assert plan.selected_default_base_sha == local_sha
        assert any("could not be verified" in warning for warning in plan.warnings)

    def test_claim_plan_linked_nested_uses_canonical_root_and_target(
        self,
        git_repo: Path,
        tmp_path: Path,
    ) -> None:
        default = _default_branch(git_repo)
        linked = tmp_path / "linked worktree"
        _git(git_repo, "worktree", "add", "-b", "linked-caller", str(linked), default)
        nested = linked / "nested" / "deeper"
        nested.mkdir(parents=True)
        (nested / "dirty.txt").write_text("dirty caller\n", encoding="utf-8")

        linked_plan = resolve_claim_plan(
            "prd:T006",
            "Linked",
            cwd=nested,
            worktree=True,
        )
        main_plan = resolve_claim_plan(
            "prd:T006",
            "Linked",
            cwd=git_repo,
            worktree=True,
        )

        assert canonical_git_root(nested) == git_repo.resolve()
        assert linked_plan.canonical_root == str(git_repo.resolve())
        assert linked_plan.caller_worktree_path == str(linked.resolve())
        assert linked_plan.linked_worktree is True
        assert linked_plan.target_path == main_plan.target_path
        assert Path(linked_plan.target_path or "").name == "wt-prd-t006"

    def test_claim_plan_linked_shared_requires_explicit_clean_authorization(
        self,
        git_repo: Path,
        tmp_path: Path,
    ) -> None:
        linked = tmp_path / "linked"
        _git(git_repo, "worktree", "add", "-b", "linked", str(linked), "HEAD")

        with pytest.raises(ClaimPlanError) as exc:
            resolve_claim_plan("T007", "Linked shared", cwd=linked)
        assert exc.value.code == "linked_shared_tree_not_authorized"

        plan = resolve_claim_plan(
            "T007",
            "Linked shared",
            cwd=linked,
            shared_tree=True,
        )
        assert plan.mode == "shared"
        assert plan.target_path == str(linked.resolve())
        assert any(item.kind == "caller_clean" for item in plan.revalidation_preconditions)

    def test_claim_plan_named_branch_uses_exact_tip_and_refuses_stale(
        self,
        git_repo: Path,
    ) -> None:
        default = _default_branch(git_repo)
        _git(git_repo, "checkout", "-b", "named-claim")
        named_sha = _commit_file(git_repo, "named.txt", "named\n", "named")
        _git(git_repo, "checkout", default)

        plan = resolve_claim_plan(
            "T008",
            "Named",
            cwd=git_repo,
            branch="named-claim",
            worktree=True,
        )

        assert plan.branch_exists is True
        assert plan.claim_start_ref == "refs/heads/named-claim"
        assert plan.claim_start_sha == named_sha
        assert plan.selected_default_base_sha != named_sha

        _commit_file(git_repo, "advance.txt", "advance\n", "advance default")
        with pytest.raises(ClaimPlanError, match="does not contain") as exc:
            resolve_claim_plan(
                "T008",
                "Named",
                cwd=git_repo,
                branch="named-claim",
                worktree=True,
            )
        assert exc.value.code == "branch_stale_or_diverged"

    def test_claim_plan_branch_owner_must_match_target(
        self,
        git_repo: Path,
        tmp_path: Path,
    ) -> None:
        owner = tmp_path / "branch-owner"
        _git(git_repo, "worktree", "add", "-b", "owned", str(owner), "HEAD")

        with pytest.raises(ClaimPlanError, match="incompatible") as exc:
            resolve_claim_plan(
                "T009",
                "Owned",
                cwd=git_repo,
                branch="owned",
                worktree=True,
            )
        assert exc.value.code == "branch_checked_out_elsewhere"

        plan = resolve_claim_plan(
            "T009",
            "Owned",
            cwd=git_repo,
            branch="owned",
            worktree=True,
            target_path=owner,
        )
        assert plan.branch_owner_path == str(owner.resolve())
        assert plan.target_owner_branch == "refs/heads/owned"

    def test_claim_plan_collision_matches_branch_creator(self, git_repo: Path) -> None:
        base_name = branch_name_for_task("T010", "x" * 100)
        _git(git_repo, "branch", base_name)

        plan = resolve_claim_plan("T010", "x" * 100, cwd=git_repo, worktree=True)
        created = create_branch_for_task(
            "T010",
            "x" * 100,
            cwd=git_repo,
            checkout=False,
        )

        assert plan.branch == created.branch
        assert plan.branch is not None and plan.branch.endswith("-2")
        assert len(plan.branch) <= 80


@pytest.mark.slow
class TestApplyClaimPlan:
    def test_isolated_apply_preserves_caller_and_compensates_owned_artifacts(
        self, git_repo: Path
    ) -> None:
        original_ref = _default_branch(git_repo)
        original_sha = _git(git_repo, "rev-parse", "HEAD")
        plan = resolve_claim_plan(
            "T012", "Transactional", cwd=git_repo, worktree=True
        )

        metadata = claim_git_metadata(plan)
        mutation = apply_claim_plan(plan, cwd=git_repo)

        assert metadata is not None
        assert metadata.claim_start_sha == original_sha
        assert metadata.worktree_path == plan.target_path
        assert _default_branch(git_repo) == original_ref
        assert _git(git_repo, "rev-parse", "HEAD") == original_sha
        assert Path(plan.target_path or "").is_dir()
        assert _git(Path(plan.target_path or ""), "rev-parse", "HEAD") == original_sha

        compensate_claim_plan(mutation, cwd=git_repo)

        assert not Path(plan.target_path or "").exists()
        assert not _ref_exists(git_repo, f"refs/heads/{plan.branch}")
        assert _default_branch(git_repo) == original_ref
        assert _git(git_repo, "rev-parse", "HEAD") == original_sha

    def test_shared_apply_restores_checkout_and_branch_on_compensation(
        self, git_repo: Path
    ) -> None:
        original_ref = _default_branch(git_repo)
        original_sha = _git(git_repo, "rev-parse", "HEAD")
        plan = resolve_claim_plan("T013", "Shared", cwd=git_repo)

        mutation = apply_claim_plan(plan, cwd=git_repo)

        assert _default_branch(git_repo) == plan.branch
        assert _git(git_repo, "rev-parse", "HEAD") == plan.claim_start_sha

        compensate_claim_plan(mutation, cwd=git_repo)

        assert _default_branch(git_repo) == original_ref
        assert _git(git_repo, "rev-parse", "HEAD") == original_sha

    def test_ref_or_target_change_refuses_before_mutation(
        self, git_repo: Path
    ) -> None:
        plan = resolve_claim_plan("T014", "Race", cwd=git_repo, worktree=True)
        assert plan.branch is not None
        _git(git_repo, "branch", plan.branch)

        with pytest.raises(ClaimPlanError) as exc:
            revalidate_claim_plan(plan, cwd=git_repo)

        assert exc.value.code == "claim_plan_changed"
        assert not Path(plan.target_path or "").exists()

    def test_occupied_target_after_plan_refuses_without_branch(
        self, git_repo: Path
    ) -> None:
        plan = resolve_claim_plan("T015", "Occupied", cwd=git_repo, worktree=True)
        target = Path(plan.target_path or "")
        target.mkdir()

        with pytest.raises(ClaimPlanError) as exc:
            apply_claim_plan(plan, cwd=git_repo)

        assert exc.value.code == "claim_plan_changed"
        assert not _ref_exists(git_repo, f"refs/heads/{plan.branch}")

    def test_explicit_target_outside_placement_root_refuses(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path.parent / "outside-claim-worktree"

        with pytest.raises(ClaimPlanError) as exc:
            resolve_claim_plan(
                "T016",
                "Outside",
                cwd=git_repo,
                worktree=True,
                target_path=outside,
            )

        assert exc.value.code == "target_outside_root"


@pytest.mark.slow
class TestResolveBase:
    def test_no_remote_degrades_to_local_default(self, git_repo: Path) -> None:
        """AC: fixture repo with no remote → local default branch,
        remote_checked=False, a reason string, no exception."""
        base = resolve_base(git_repo)
        assert base.ref == _default_branch(git_repo)
        assert base.remote_checked is False
        assert base.reason  # non-empty explanation
        assert isinstance(base, BaseRef)

    def test_not_a_repo_returns_none_ref(self, tmp_path: Path) -> None:
        base = resolve_base(tmp_path)  # exists, but not a repo
        assert base.ref is None
        assert base.remote_checked is False
        assert base.reason

    def test_nonexistent_dir_returns_none_ref_without_raising(
        self, tmp_path: Path
    ) -> None:
        base = resolve_base(tmp_path / "not-a-repo-anywhere")
        assert base.ref is None
        assert base.remote_checked is False
        assert "does not exist" in (base.reason or "")

    def test_unreachable_remote_degrades_with_fetch_reason(
        self, git_repo: Path
    ) -> None:
        """origin exists but the fetch fails → local base, reason names it."""
        _git(git_repo, "remote", "add", "origin", str(git_repo / "nope.git"))
        base = resolve_base(git_repo)
        assert base.ref == _default_branch(git_repo)
        assert base.remote_checked is False
        assert "fetch failed" in (base.reason or "")


@pytest.mark.slow
class TestCheckFreshness:
    def test_up_to_date_branch_reports_zero_behind(self, git_repo: Path) -> None:
        _git(git_repo, "branch", "feature")
        report = check_freshness("feature", cwd=git_repo)
        assert report.behind_count == 0
        assert report.is_stale is False
        assert report.has_conflicts is False

    def test_branch_two_behind_reports_two(self, git_repo: Path) -> None:
        """AC: a branch 2 commits behind base reports behind_count == 2."""
        _git(git_repo, "branch", "feature")
        for i in range(2):
            (git_repo / f"file{i}.txt").write_text(f"{i}\n", encoding="utf-8")
            _git(git_repo, "add", ".")
            _git(git_repo, "commit", "-m", f"advance {i}")
        report = check_freshness("feature", cwd=git_repo)
        assert report.behind_count == 2
        assert report.is_stale is True
        assert report.has_conflicts is False  # disjoint files merge cleanly

    def test_textual_conflict_detected(self, git_repo: Path) -> None:
        """AC: a branch that conflicts with base reports has_conflicts=True."""
        default = _default_branch(git_repo)
        _git(git_repo, "checkout", "-b", "feature")
        (git_repo / "README.md").write_text("feature line\n", encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "feature edit")
        _git(git_repo, "checkout", default)
        (git_repo / "README.md").write_text("main line\n", encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "main edit")
        report = check_freshness("feature", cwd=git_repo)
        assert report.has_conflicts is True
        assert report.conflict_probe == "merge-tree"

    def test_old_git_probe_skipped_not_failed(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC: without merge-tree support the probe is skipped, not failed."""
        _git(git_repo, "branch", "feature")
        real_run_git = freshness_mod._run_git

        def fake_run_git(args: list[str], cwd: Path):  # type: ignore[no-untyped-def]
            if args and args[0] == "merge-tree":
                return subprocess.CompletedProcess(
                    args=["git", *args],
                    returncode=129,
                    stdout="",
                    stderr="error: unknown option `write-tree'",
                )
            return real_run_git(args, cwd)

        monkeypatch.setattr(freshness_mod, "_run_git", fake_run_git)
        report = check_freshness("feature", cwd=git_repo)
        assert report.has_conflicts is None
        assert report.conflict_probe.startswith("skipped:")
        assert report.behind_count == 0  # freshness half still ran

    def test_caller_supplied_stale_base_ref_skipped_not_conflict(
        self, git_repo: Path
    ) -> None:
        """Review finding: merge-tree exits 1 for a missing ref too — a
        caller-supplied BaseRef naming a deleted ref must read as skipped,
        never as a false has_conflicts=True."""
        _git(git_repo, "branch", "feature")
        stale = BaseRef(ref="gone-branch", remote_checked=False, reason=None)
        report = check_freshness("feature", cwd=git_repo, base=stale)
        assert report.has_conflicts is None
        assert report.conflict_probe == "skipped: base ref missing"
        assert "not found" in (report.reason or "")

    def test_missing_branch_reports_reason(self, git_repo: Path) -> None:
        report = check_freshness("no-such-branch", cwd=git_repo)
        assert report.behind_count is None
        assert report.has_conflicts is None
        assert "not found" in (report.reason or "")

    def test_never_writes_working_tree(self, git_repo: Path) -> None:
        """AC: no function in the module writes to the repo working tree."""
        default = _default_branch(git_repo)
        _git(git_repo, "checkout", "-b", "feature")
        (git_repo / "README.md").write_text("feature line\n", encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "feature edit")
        _git(git_repo, "checkout", default)
        (git_repo / "README.md").write_text("main line\n", encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "main edit")

        resolve_base(git_repo)
        check_freshness("feature", cwd=git_repo)

        assert _git(git_repo, "status", "--porcelain") == ""
        assert _default_branch(git_repo) == default
