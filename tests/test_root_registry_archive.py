"""Archived roots are history; nonterminal reservations retain live custody."""

import shutil

import pytest

from anvil.cli import app
from anvil.roots.registry import RootSetError
from tests.test_root_sets import _git, _json, _repo, runner
from tests.test_root_sets import idle_enrollment as idle_enrollment


@pytest.fixture
def archived_alias(idle_enrollment, tmp_path):
    repo, args, registry = idle_enrollment
    alias = tmp_path / "archived"
    _git(repo, "worktree", "add", "-qb", "archive", str(alias))
    changed = args.copy()
    changed[changed.index(str(repo))] = str(alias)
    _json(runner.invoke(app, changed, catch_exceptions=False))
    return repo, alias, registry


@pytest.mark.parametrize("state", ["released", "pending", "bound", "release_pending"])
def test_missing_archived_root_preserves_history_and_active_custody(archived_alias, state):
    repo, alias, registry = archived_alias
    with registry.locked() as data:
        record = registry.reserve(data, request_id="old", digest="a" * 64,
            actor="owner", state_identity=str(repo / ".anvil"),
            roots=[{"root_id": "owner", "repository_id": "owner", "path": str(alias)}])
        record.update(state=state, claim_id="COLD")
    _git(repo, "worktree", "remove", str(alias))
    before = registry.path.read_bytes()
    if state != "released":
        with pytest.raises(RootSetError, match="registry is corrupt"):
            with registry.locked():
                pytest.fail("a nonterminal missing root was accepted")
        assert registry.path.read_bytes() == before
        return
    with registry.locked() as data:
        assert data["reservations"]["old"]["state"] == "released"
    assert registry.path.read_bytes() == before
    with registry.locked() as data:
        registry.reserve(data, request_id="next", digest="b" * 64,
            actor="owner", state_identity=str(repo / ".anvil"),
            roots=[{"root_id": "owner", "repository_id": "owner", "path": str(repo)}])
    with registry.locked() as data:
        assert data["reservations"]["next"]["state"] == "pending"
        with pytest.raises(RootSetError, match="reserved"):
            registry.reserve(data, request_id="conflict", digest="c" * 64,
                actor="other", state_identity=str(repo / ".anvil"),
                roots=[{"root_id": "owner", "repository_id": "owner", "path": str(repo)}])


@pytest.mark.parametrize("replacement", ["symlink", "different_git"])
def test_archived_alias_reuse_does_not_grant_live_identity(archived_alias, tmp_path, replacement):
    repo, alias, registry = archived_alias
    _git(repo, "worktree", "remove", str(alias))
    other = _repo(tmp_path / "other")
    if replacement == "symlink":
        alias.symlink_to(other, target_is_directory=True)
    else:
        shutil.copytree(other, alias)
    before = registry.path.read_bytes()
    with pytest.raises(RootSetError):
        with registry.locked() as data:
            registry.reserve(data, request_id="reused", digest="d" * 64,
                actor="owner", state_identity=str(repo / ".anvil"),
                roots=[{"root_id": "owner", "repository_id": "owner", "path": str(alias)}])
    assert registry.path.read_bytes() == before
