"""The HOME-workspace state layout: one shared state.db per project across all
git worktrees, in the user's home — fixing state stranded inside individual
worktrees. ``ANVIL_ROOT`` stays a literal override; ``ANVIL_STATE_LAYOUT=local``
keeps the legacy in-repo ``<cwd>/.anvil`` (what the rest of the suite uses)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from anvil.cli._helpers import _resolve_state_dir


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate HOME and select the production (workspace) layout for these tests
    (the suite-wide autouse fixture pins `local`)."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", lambda: h)
    monkeypatch.delenv("ANVIL_ROOT", raising=False)
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
    return h


def test_worktrees_share_one_home_workspace(
    home: Path, tmp_path: Path
) -> None:
    """The main checkout and a git worktree of the same repo resolve to the SAME
    `~/.anvil/workspaces/<repo>/.anvil` — the whole point of the change."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feature")

    expected = home / ".anvil" / "workspaces" / "myrepo" / ".anvil"
    assert _resolve_state_dir(repo) == expected
    assert _resolve_state_dir(wt) == expected  # the worktree shares it


def test_local_layout_keeps_state_in_repo(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANVIL_STATE_LAYOUT=local → legacy `<cwd>/.anvil` (no home workspace)."""
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "local")
    proj = tmp_path / "proj"
    proj.mkdir()
    assert _resolve_state_dir(proj) == proj / ".anvil"


def test_anvil_root_is_a_literal_override(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANVIL_ROOT always wins and stays literal, even in workspace layout."""
    root = tmp_path / "explicit"
    (root / ".anvil").mkdir(parents=True)
    monkeypatch.setenv("ANVIL_ROOT", str(root))
    assert _resolve_state_dir(None) == root / ".anvil"


def test_non_git_dir_falls_back_to_its_own_name(home: Path, tmp_path: Path) -> None:
    """Outside a git repo the workspace is keyed by the dir's own name."""
    plain = tmp_path / "loose"
    plain.mkdir()
    assert _resolve_state_dir(plain) == home / ".anvil" / "workspaces" / "loose" / ".anvil"
