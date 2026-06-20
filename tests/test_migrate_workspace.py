"""Tests for ``anvil migrate-workspace`` (B44) — one-time migration of legacy
in-repo ``.anvil/`` state into the HOME workspace.

Safety-first invariants under test: dry-run by default, no-clobber of an existing
home workspace, copy-not-move (legacy survives), idempotency, and the
``bin/.anvil`` dogfooding fallback. All run under the production (workspace)
layout (the suite-wide autouse fixture pins ``local``, so we opt back in here).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", lambda: h)
    monkeypatch.delenv("ANVIL_ROOT", raising=False)
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
    return h


def _legacy(repo: Path, *, sub: str = "") -> Path:
    """Create a legacy in-repo .anvil/ with a state.db (+ a wal sidecar) and a
    sub-file, returning the .anvil dir."""
    anvil = (repo / sub / ".anvil") if sub else (repo / ".anvil")
    (anvil / "packets").mkdir(parents=True)
    (anvil / "state.db").write_text("LEGACY-DB")
    (anvil / "state.db-wal").write_text("WAL")
    (anvil / "events.jsonl").write_text('{"e":1}\n')
    return anvil


def _run(repo: Path, *args: str):  # noqa: ANN202
    return runner.invoke(
        app, ["migrate-workspace", "--json", "--cwd", str(repo), *args], catch_exceptions=False
    )


def test_dry_run_writes_nothing(home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    _legacy(repo)
    r = _run(repo)  # no --yes → dry run
    assert r.exit_code == 0
    data = json.loads(r.stdout)["data"]
    assert data["status"] == "dry_run"
    assert data["applied"] is False
    assert not Path(data["target"]).exists()  # nothing written


def test_apply_copies_into_home_and_keeps_legacy(home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    legacy = _legacy(repo)
    r = _run(repo, "--yes")
    assert r.exit_code == 0
    data = json.loads(r.stdout)["data"]
    assert data["status"] == "migrated"
    target = Path(data["target"])
    assert (target / "state.db").read_text() == "LEGACY-DB"
    assert (target / "state.db-wal").read_text() == "WAL"  # WAL sidecar copied too
    assert (target / "packets").is_dir()
    assert (legacy / "state.db").exists()  # copy, NOT move — legacy survives


def test_no_clobber_of_existing_home_workspace(home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    _legacy(repo)
    # Pre-create a home workspace with sentinel content → must NOT be overwritten.
    dry = json.loads(_run(repo).stdout)["data"]
    target = Path(dry["target"])
    (target).mkdir(parents=True)
    (target / "state.db").write_text("HOME-AUTHORITATIVE")
    r = _run(repo, "--yes")
    assert r.exit_code == 0
    data = json.loads(r.stdout)["data"]
    assert data["status"] == "already_migrated"
    assert data["applied"] is False
    assert (target / "state.db").read_text() == "HOME-AUTHORITATIVE"  # untouched


def test_idempotent(home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    _legacy(repo)
    first = json.loads(_run(repo, "--yes").stdout)["data"]
    assert first["status"] == "migrated"
    second = json.loads(_run(repo, "--yes").stdout)["data"]
    assert second["status"] == "already_migrated"  # second run is a no-op


def test_bin_anvil_fallback(home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    _legacy(repo, sub="bin")  # legacy only at <repo>/bin/.anvil
    r = _run(repo, "--yes")
    data = json.loads(r.stdout)["data"]
    assert data["status"] == "migrated"
    assert data["source"].endswith("/bin/.anvil")
    assert (Path(data["target"]) / "state.db").read_text() == "LEGACY-DB"


def test_no_legacy_state(home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()  # no .anvil anywhere
    r = _run(repo, "--yes")
    assert r.exit_code == 0
    data = json.loads(r.stdout)["data"]
    assert data["status"] == "no_legacy_state"
    assert not Path(data["target"]).exists()


def test_migrated_project_resolves_to_target(home: Path, tmp_path: Path) -> None:
    """Round-trip: after --yes the project RESOLVES its state to the migrated dir
    (pins migrate ↔ _resolve_state_dir agreement across keying changes)."""
    from anvil.cli._helpers import _resolve_state_dir

    repo = tmp_path / "proj"
    repo.mkdir()
    _legacy(repo)
    data = json.loads(_run(repo, "--yes").stdout)["data"]
    target = Path(data["target"])
    assert _resolve_state_dir(repo) == target  # the engine reads back what migrate wrote
    assert (target / "state.db").read_text() == "LEGACY-DB"


def test_finds_worktree_stranded_state(home: Path, tmp_path: Path) -> None:
    """Legacy state stranded INSIDE a non-main worktree (not the canonical root) is
    found when migrate runs from that worktree — the exact case the layout fixes."""
    import subprocess

    def _git(cwd: Path, *a: str) -> None:
        subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@anvil.test")
    _git(repo, "config", "user.name", "anvil-test")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    (wt / ".anvil").mkdir()
    (wt / ".anvil" / "state.db").write_text("WT-STRANDED-DB")  # stranded in the worktree

    r = runner.invoke(
        app, ["migrate-workspace", "--json", "--yes", "--cwd", str(wt)], catch_exceptions=False
    )
    data = json.loads(r.stdout)["data"]
    assert data["status"] == "migrated"
    assert (Path(data["target"]) / "state.db").read_text() == "WT-STRANDED-DB"
