"""The HOME-workspace state layout: one shared state.db per project across all
git worktrees, in the user's home — fixing state stranded inside individual
worktrees. ``ANVIL_ROOT`` stays a literal override; ``ANVIL_STATE_LAYOUT=local``
keeps the legacy in-repo ``<cwd>/.anvil`` (what the rest of the suite uses)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from anvil.cli._helpers import (
    _canonical_project_root,
    _resolve_state_dir,
    _workspace_key,
)


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


@pytest.mark.slow
def test_worktrees_share_one_home_workspace(
    home: Path, tmp_path: Path
) -> None:
    """The main checkout and a git worktree of the same repo resolve to the SAME
    `~/.anvil/workspaces/<repo>/.anvil` — the whole point of the change."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@anvil.test")  # CI has no global identity
    _git(repo, "config", "user.name", "anvil-test")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feature")

    # B44 dual-key: a NEW project (no pre-existing bare-name workspace) resolves to
    # the collision-proof hashed key. Both worktrees share it (same canonical root).
    key = _workspace_key(_canonical_project_root(repo))
    expected = home / ".anvil" / "workspaces" / key / ".anvil"
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


def test_non_git_dir_uses_hashed_key(home: Path, tmp_path: Path) -> None:
    """Outside a git repo the workspace is keyed by basename + path hash (B44)."""
    plain = tmp_path / "loose"
    plain.mkdir()
    key = _workspace_key(_canonical_project_root(plain))
    assert key.startswith("loose-")
    assert _resolve_state_dir(plain) == home / ".anvil" / "workspaces" / key / ".anvil"


# --- B44: dual-key (back-compat) + collision -------------------------------


def test_existing_bare_key_workspace_is_honored(home: Path, tmp_path: Path) -> None:
    """A pre-existing BARE-name workspace (created by the original #42 code) keeps
    resolving — never orphaned by the new hashed key."""
    plain = tmp_path / "loose"
    plain.mkdir()
    bare = home / ".anvil" / "workspaces" / "loose" / ".anvil"
    bare.mkdir(parents=True)
    (bare / "state.db").write_text("sentinel")  # an existing db under the bare key
    assert _resolve_state_dir(plain) == bare  # bare wins — no orphaning


def test_partial_bare_workspace_falls_to_hashed(home: Path, tmp_path: Path) -> None:
    """A bare-name workspace dir WITHOUT a state.db is NOT honored — resolve to the
    hashed key. The no-orphaning rule is about real dbs, not empty/partial dirs."""
    plain = tmp_path / "loose"
    plain.mkdir()
    (home / ".anvil" / "workspaces" / "loose" / ".anvil").mkdir(parents=True)  # no state.db
    key = _workspace_key(_canonical_project_root(plain))
    assert _resolve_state_dir(plain) == home / ".anvil" / "workspaces" / key / ".anvil"


def test_same_basename_different_paths_dont_collide(home: Path, tmp_path: Path) -> None:
    """Two projects sharing a basename map to DISTINCT hashed workspaces (B44 fix)."""
    a = tmp_path / "a" / "app"
    b = tmp_path / "b" / "app"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    da = _resolve_state_dir(a)
    db = _resolve_state_dir(b)
    assert da != db  # no collision despite the shared 'app' basename
