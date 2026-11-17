"""B44-1 — a legacy bare-name home workspace resolves only for the project its
owner marker names, and is adopted only by an explicit operator command.

``~/.anvil/workspaces/<basename>/`` (created by the original #42 code) is keyed
on the directory name alone. It used to be honoured for ANY checkout with that
basename, so an unrelated same-named repository read and wrote another
project's state. All tests run under the production (workspace) layout.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.cli._helpers import (
    WORKSPACE_OWNER_MARKER,
    _canonical_project_root,
    _resolve_state_dir,
    _workspace_key,
    legacy_workspace_hint,
    read_workspace_owner,
    unclaimed_legacy_workspace,
    write_workspace_owner,
)

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", lambda: h)
    monkeypatch.delenv("ANVIL_ROOT", raising=False)
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
    return h


def _project(tmp_path: Path, *parts: str) -> Path:
    project = tmp_path.joinpath(*parts)
    project.mkdir(parents=True)
    return project


def _bare(home: Path, name: str) -> Path:
    """A pre-#42 bare-name workspace with real-looking state; returns its .anvil."""
    anvil = home / ".anvil" / "workspaces" / name / ".anvil"
    anvil.mkdir(parents=True)
    (anvil / "state.db").write_text("LEGACY-DB")
    (anvil / "events.jsonl").write_text('{"e":1}\n')
    return anvil


def _hashed(home: Path, project: Path) -> Path:
    key = _workspace_key(_canonical_project_root(project))
    return home / ".anvil" / "workspaces" / key / ".anvil"


def _migrate(project: Path, *args: str):  # noqa: ANN202
    return runner.invoke(
        app,
        ["migrate-workspace", "--json", "--cwd", str(project), *args],
        catch_exceptions=False,
    )


# --- resolution -------------------------------------------------------------


def test_marker_for_another_root_is_not_honored(home: Path, tmp_path: Path) -> None:
    owner = _project(tmp_path, "a", "app")
    intruder = _project(tmp_path, "b", "app")
    bare = _bare(home, "app")
    write_workspace_owner(bare, _canonical_project_root(owner))

    assert _resolve_state_dir(owner) == bare
    assert _resolve_state_dir(intruder) == _hashed(home, intruder)
    # Bound to someone else: not adoptable, so no hint and no helper result.
    assert unclaimed_legacy_workspace(intruder) is None
    assert legacy_workspace_hint(_hashed(home, intruder)) == ""


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"[]",
        b'{"version": 1}',
        b'{"version": 2, "canonical_root": "ROOT"}',
        b'{"version": true, "canonical_root": "ROOT"}',
        b'{"version": 1, "canonical_root": 7}',
        b'{"version": 1, "canonical_root": "  "}',
        b'{"version": 1, "canonical_root": "ROOT", "pad": "' + b"x" * 5000 + b'"}',
        b"\xff\xfe",
    ],
)
def test_malformed_marker_is_not_honored(home: Path, tmp_path: Path, content: bytes) -> None:
    project = _project(tmp_path, "app")
    bare = _bare(home, "app")
    root = str(_canonical_project_root(project)).encode()
    (bare / WORKSPACE_OWNER_MARKER).write_bytes(content.replace(b"ROOT", root))

    assert read_workspace_owner(bare) == ("invalid", None)
    assert _resolve_state_dir(project) == _hashed(home, project)
    # An invalid marker is not "unclaimed": it needs an operator's eyes first.
    assert unclaimed_legacy_workspace(project) is None


def test_symlinked_marker_is_not_honored(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path, "app")
    bare = _bare(home, "app")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    write_workspace_owner(elsewhere, _canonical_project_root(project))  # a VALID marker
    try:
        (bare / WORKSPACE_OWNER_MARKER).symlink_to(elsewhere / WORKSPACE_OWNER_MARKER)
    except OSError:
        pytest.skip("native symlink creation unavailable")

    assert read_workspace_owner(bare) == ("invalid", None)
    assert _resolve_state_dir(project) == _hashed(home, project)


def test_directory_named_like_the_marker_is_not_honored(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path, "app")
    bare = _bare(home, "app")
    (bare / WORKSPACE_OWNER_MARKER).mkdir()

    assert read_workspace_owner(bare) == ("invalid", None)
    assert _resolve_state_dir(project) == _hashed(home, project)


def test_project_named_like_a_hashed_key_does_not_reach_that_workspace(
    home: Path, tmp_path: Path
) -> None:
    """A hashed workspace has no marker, so a checkout whose basename equals
    another project's hashed key must not resolve into it either."""
    victim = _project(tmp_path, "victim")
    victim_state = _hashed(home, victim)
    victim_state.mkdir(parents=True)
    (victim_state / "state.db").write_text("VICTIM")
    impostor = _project(tmp_path, "x", victim_state.parent.name)

    assert _resolve_state_dir(impostor) != victim_state
    assert _resolve_state_dir(victim) == victim_state


# --- adoption ---------------------------------------------------------------


def test_adopt_dry_run_reports_and_writes_nothing(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path, "app")
    bare = _bare(home, "app")

    result = _migrate(project, "--adopt-legacy")

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["status"] == "adopt_dry_run" and data["applied"] is False
    assert data["legacy_workspace"] == bare.parent.as_posix()
    assert data["canonical_root"] == _canonical_project_root(project).as_posix()
    assert data["events_modified_at"] is not None
    assert not (bare / WORKSPACE_OWNER_MARKER).exists()
    assert _resolve_state_dir(project) == _hashed(home, project)


def test_adopt_apply_binds_the_workspace_to_this_project_only(
    home: Path, tmp_path: Path
) -> None:
    project = _project(tmp_path, "a", "app")
    other = _project(tmp_path, "b", "app")
    bare = _bare(home, "app")

    result = _migrate(project, "--adopt-legacy", "--yes")

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["status"] == "adopted" and data["applied"] is True
    marker = bare / WORKSPACE_OWNER_MARKER
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "version": 1,
        "canonical_root": str(_canonical_project_root(project)),
    }
    if os.name != "nt":
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert sorted(p.name for p in bare.iterdir()) == [
        "events.jsonl", "state.db", WORKSPACE_OWNER_MARKER,
    ]  # no temp file left behind; state untouched
    assert (bare / "state.db").read_text() == "LEGACY-DB"
    assert _resolve_state_dir(project) == bare
    assert _resolve_state_dir(other) == _hashed(home, other)

    again = _migrate(project, "--adopt-legacy", "--yes")
    assert again.exit_code == 0
    assert json.loads(again.stdout)["data"]["status"] == "already_adopted"


def test_adopt_refuses_a_workspace_bound_to_another_project(
    home: Path, tmp_path: Path
) -> None:
    owner = _project(tmp_path, "a", "app")
    intruder = _project(tmp_path, "b", "app")
    bare = _bare(home, "app")
    write_workspace_owner(bare, _canonical_project_root(owner))
    before = (bare / WORKSPACE_OWNER_MARKER).read_bytes()

    result = _migrate(intruder, "--adopt-legacy", "--yes")

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "legacy_workspace_owned"
    assert (bare / WORKSPACE_OWNER_MARKER).read_bytes() == before
    assert _resolve_state_dir(owner) == bare


def test_owner_marker_is_never_replaced(home: Path, tmp_path: Path) -> None:
    first = _project(tmp_path, "a", "app")
    second = _project(tmp_path, "b", "app")
    bare = _bare(home, "app")
    write_workspace_owner(bare, _canonical_project_root(first))
    before = (bare / WORKSPACE_OWNER_MARKER).read_bytes()

    with pytest.raises(FileExistsError):
        write_workspace_owner(bare, _canonical_project_root(second))

    assert (bare / WORKSPACE_OWNER_MARKER).read_bytes() == before
    assert sorted(p.name for p in bare.iterdir()) == [
        "events.jsonl", "state.db", WORKSPACE_OWNER_MARKER,
    ]


def test_adopt_loses_cleanly_to_a_concurrent_adoption(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two same-named projects adopt at once: one owner, and the loser is told."""
    import sys

    # `anvil.cli.migrate_workspace` the attribute is the command function.
    module = sys.modules["anvil.cli.migrate_workspace"]

    winner = _project(tmp_path, "a", "app")
    loser = _project(tmp_path, "b", "app")
    bare = _bare(home, "app")
    real_write = module.write_workspace_owner

    def publish_after_the_winner(anvil_dir: Path, root: Path) -> None:
        # The other adoption lands between this command's check and its write.
        real_write(anvil_dir, _canonical_project_root(winner))
        real_write(anvil_dir, root)

    monkeypatch.setattr(module, "write_workspace_owner", publish_after_the_winner)

    result = _migrate(loser, "--adopt-legacy", "--yes")

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "legacy_workspace_owned"
    assert _resolve_state_dir(winner) == bare
    assert _resolve_state_dir(loser) == _hashed(home, loser)


def test_adopt_refuses_an_invalid_marker(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path, "app")
    bare = _bare(home, "app")
    (bare / WORKSPACE_OWNER_MARKER).write_text("garbage")

    result = _migrate(project, "--adopt-legacy", "--yes")

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "legacy_marker_invalid"
    assert (bare / WORKSPACE_OWNER_MARKER).read_text() == "garbage"


def test_adopt_refuses_when_the_project_already_has_a_workspace(
    home: Path, tmp_path: Path
) -> None:
    project = _project(tmp_path, "app")
    bare = _bare(home, "app")
    current = _hashed(home, project)
    current.mkdir(parents=True)
    (current / "state.db").write_text("CURRENT")

    result = _migrate(project, "--adopt-legacy", "--yes")

    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "workspace_conflict"
    assert current.as_posix() in error["message"] and bare.as_posix() in error["message"]
    assert not (bare / WORKSPACE_OWNER_MARKER).exists()
    assert _resolve_state_dir(project) == current


def test_adopt_with_no_legacy_workspace_is_a_clean_no_op(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path, "app")

    result = _migrate(project, "--adopt-legacy", "--yes")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["status"] == "no_legacy_workspace"
    assert not (home / ".anvil").exists()


def test_plain_migrate_keeps_its_status_and_mentions_the_adoptable_workspace(
    home: Path, tmp_path: Path
) -> None:
    project = _project(tmp_path, "app")
    bare = _bare(home, "app")

    data = json.loads(_migrate(project).stdout)["data"]

    assert data["status"] == "no_legacy_state"  # unchanged contract
    assert data["target"] == _hashed(home, project).as_posix()
    assert data["adoptable_legacy_workspace"] == bare.parent.as_posix()
    assert "--adopt-legacy" in data["message"]

    # With nothing to adopt the envelope carries no extra key.
    clean = _project(tmp_path, "clean")
    clean_data = json.loads(_migrate(clean).stdout)["data"]
    assert "adoptable_legacy_workspace" not in clean_data
    assert "--adopt-legacy" not in clean_data["message"]


# --- discoverability --------------------------------------------------------


def test_not_initialized_messages_point_at_adoption(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path, "app")
    bare = _bare(home, "app")

    status = runner.invoke(app, ["status", "--cwd", str(project)])
    assert status.exit_code == 1
    assert "not initialized" in status.output
    assert "migrate-workspace --adopt-legacy" in status.output
    assert bare.parent.as_posix() in status.output

    status_json = runner.invoke(app, ["status", "--json", "--cwd", str(project)])
    error = json.loads(status_json.stdout)["error"]
    assert error["code"] == "not_initialized"
    assert "migrate-workspace --adopt-legacy" in error["message"]

    drift = runner.invoke(app, ["drift", "--json", "--cwd", str(project)])
    drift_error = json.loads(drift.stdout)["error"]
    assert drift_error["code"] == "not_initialized"
    assert "migrate-workspace --adopt-legacy" in drift_error["message"]

    session = runner.invoke(
        app, ["hook", "dispatch", "detect-state", "--cwd", str(project)], input="{}"
    )
    assert session.exit_code == 0
    context = json.loads(session.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "not initialized" in context
    assert "migrate-workspace --adopt-legacy" in context


def test_no_hint_without_an_unclaimed_legacy_workspace(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path, "app")

    status = runner.invoke(app, ["status", "--cwd", str(project)])
    assert status.exit_code == 1
    assert "not initialized" in status.output
    assert "adopt-legacy" not in status.output

    session = runner.invoke(
        app, ["hook", "dispatch", "detect-state", "--cwd", str(project)], input="{}"
    )
    context = json.loads(session.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "adopt-legacy" not in context
