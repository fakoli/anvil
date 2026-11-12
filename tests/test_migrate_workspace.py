"""Tests for ``anvil migrate-workspace`` (B44) — one-time migration of legacy
in-repo ``.anvil/`` state into the HOME workspace.

Safety-first invariants under test: dry-run by default, no-clobber of an existing
home workspace, copy-not-move (legacy survives), idempotency, and the
``bin/.anvil`` dogfooding fallback. All run under the production (workspace)
layout (the suite-wide autouse fixture pins ``local``, so we opt back in here).
"""

from __future__ import annotations

import json
import os
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


@pytest.mark.slow
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


def test_workspace_copy_preserves_retained_sqlite_locks_and_later_commit(
    home: Path, tmp_path: Path,
) -> None:
    import os

    from tests.test_sqlite_file_safety import (
        _SQLITE_OBSERVER,
        _observe_in_child,
        _require_retained_lock,
        _retained_database,
    )

    repo = tmp_path / "retained-project"
    state = repo / ".anvil"
    state.mkdir(parents=True)
    with _retained_database(state) as (database, _, retained):
        result = _run(repo, "--yes")
        assert result.exit_code == 0, result.output
        target = Path(json.loads(result.stdout)["data"]["target"])
        if os.name == "posix":
            _require_retained_lock(database)
        assert _observe_in_child(_SQLITE_OBSERVER, database)["rows"] == [["before-inspection"]]
        retained.execute("INSERT INTO retained_owner_probe VALUES ('after-copy')")
        retained.commit()
        assert _observe_in_child(_SQLITE_OBSERVER, database)["rows"] == [
            ["before-inspection"], ["after-copy"],
        ]
        assert _observe_in_child(_SQLITE_OBSERVER, target / "state.db")["rows"] == [
            ["before-inspection"],
        ]


def test_workspace_partial_child_copy_failure_removes_staging(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from anvil.state.backend import SchemaProbeFailed
    from anvil.state.file_inspection import BoundedFileInspection
    from tests.test_cli import _recursive_file_manifest

    repo = tmp_path / "failed-copy"
    repo.mkdir()
    legacy = _legacy(repo)
    before = _recursive_file_manifest(legacy)
    destinations = []

    def failed_copy(self, source, destination):
        destination.mkdir()
        (destination / "state.db").write_bytes(b"partial")
        destinations.append(destination)
        raise SchemaProbeFailed("injected isolated copy failure")

    monkeypatch.setattr(BoundedFileInspection, "copy_tree", failed_copy)
    result = _run(repo, "--yes")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "migration_failed"
    assert destinations and all(not path.exists() for path in destinations)
    assert _recursive_file_manifest(legacy) == before


def _assert_pending_copy_refuses_without_changes(repo: Path, source: Path) -> None:
    from anvil.cli._helpers import _home_workspace_base
    from tests.test_cli import _recursive_file_manifest

    target_base = _home_workspace_base(repo)
    before = _recursive_file_manifest(source)
    identities = {
        path: (path.lstat().st_dev, path.lstat().st_ino)
        for path in [source, *source.rglob("*")]
    }
    assert not target_base.exists()
    for arguments in ((), ("--yes",)):
        result = _run(repo, *arguments)
        assert result.exit_code == 1, result.output
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "migration_failed"
        assert "--resume" in error["message"]
        assert not target_base.exists()
        assert _recursive_file_manifest(source) == before
        assert {
            path: (path.lstat().st_dev, path.lstat().st_ino)
            for path in [source, *source.rglob("*")]
        } == identities


@pytest.mark.parametrize("recovery", ["after_marker", "after_database", "local-event"])
def test_workspace_refuses_pending_native_recovery_then_copies_resumed_state(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str,
) -> None:
    import anvil.cli.migrate as migration
    from tests.test_git_events import _build_local_project, _run_in
    from tests.test_local_event_repair import _pending, _resume_args, _state

    if os.name == "nt" and recovery != "local-event":
        pytest.skip("Event-format mutation is explicitly unsupported on Windows")
    repo = tmp_path / "pending-project"
    repo.mkdir()
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "local")
    if recovery == "local-event":
        state, receipt = _state(repo)
        marker = _pending(repo, receipt, monkeypatch)
        resume_arguments = _resume_args(repo)
    else:
        _build_local_project(repo)
        state = repo / ".anvil"

        def interrupt(phase):
            if phase == recovery:
                raise RuntimeError("injected conversion interruption")

        with monkeypatch.context() as interrupted:
            interrupted.setattr(migration, "_EVENT_CONVERSION_PHASE_HOOK", interrupt)
            result = _run_in(repo, ["migrate-events", "--to", "git", "--yes"])
        assert result.exit_code == 1, result.output
        marker = state / ".event-format-migration.json"
        resume_arguments = ["migrate-events", "--to", "git", "--resume", "--yes"]
    assert marker.is_file()
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
    _assert_pending_copy_refuses_without_changes(repo, state)

    # Recovery must remain possible only at its original physical identities.
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "local")
    resumed = _run_in(repo, resume_arguments)
    assert resumed.exit_code == 0, resumed.output
    assert not marker.exists()
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
    copied = _run(repo, "--yes")
    assert copied.exit_code == 0, copied.output
    assert json.loads(copied.stdout)["data"]["status"] == "migrated"
    snapshot = _run_in(repo, ["project", "snapshot", "--json"])
    assert snapshot.exit_code == 0, snapshot.output
    assert json.loads(snapshot.stdout)["ok"] is True


@pytest.mark.parametrize("marker_name", [
    ".event-format-migration.json", ".local-event-recovery.json",
])
@pytest.mark.parametrize("marker_kind", ["broken-link", "directory"])
def test_workspace_unsafe_recovery_marker_is_not_treated_as_absent(
    home: Path, tmp_path: Path, marker_name: str, marker_kind: str,
) -> None:
    if os.name == "nt" and marker_kind == "broken-link":
        pytest.skip("Creating Windows symlinks requires separate host privileges")
    repo = tmp_path / "unsafe-marker"
    repo.mkdir()
    state = _legacy(repo)
    marker = state / marker_name
    if marker_kind == "broken-link":
        marker.symlink_to(state / "missing-recovery-record")
    else:
        marker.mkdir()
    _assert_pending_copy_refuses_without_changes(repo, state)


@pytest.mark.parametrize("marker_name", [
    ".event-format-migration.json", ".local-event-recovery.json",
])
@pytest.mark.parametrize("location", ["source", "staging"])
def test_workspace_rechecks_recovery_after_copy_and_removes_only_staging(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    marker_name: str, location: str,
) -> None:
    from anvil.cli._helpers import _home_workspace_base
    from anvil.state.file_inspection import BoundedFileInspection
    from tests.test_cli import _recursive_file_manifest

    repo = tmp_path / "late-recovery"
    repo.mkdir()
    state = _legacy(repo)
    before = _recursive_file_manifest(state)
    copy_tree = BoundedFileInspection.copy_tree
    destinations = []

    def copy_then_publish_marker(self, source, destination):
        copy_tree(self, source, destination)
        destinations.append(destination)
        marker = (source if location == "source" else destination) / marker_name
        marker.write_bytes(b"retained pending recovery")

    monkeypatch.setattr(BoundedFileInspection, "copy_tree", copy_then_publish_marker)
    result = _run(repo, "--yes")
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "migration_failed"
    assert not (_home_workspace_base(repo) / ".anvil").exists()
    assert destinations and all(not path.exists() for path in destinations)
    expected = {**before, marker_name: b"retained pending recovery"} if location == "source" else before
    assert _recursive_file_manifest(state) == expected


def test_workspace_token_appearing_after_legacy_admission_refuses_publication(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from anvil.cli._helpers import _home_workspace_base
    from anvil.state.file_inspection import BoundedFileInspection
    from tests.test_cli import _recursive_file_manifest

    repo = tmp_path / "late-token"
    repo.mkdir()
    state = _legacy(repo)
    before = _recursive_file_manifest(state)
    copy_tree = BoundedFileInspection.copy_tree

    def copy_then_create_token(self, source, destination):
        copy_tree(self, source, destination)
        (source / ".state-operation.lock").write_bytes(b"")

    monkeypatch.setattr(BoundedFileInspection, "copy_tree", copy_then_create_token)
    result = _run(repo, "--yes")
    assert result.exit_code == 1, result.output
    assert "coordination changed" in json.loads(result.stdout)["error"]["message"]
    target_base = _home_workspace_base(repo)
    assert not (target_base / ".anvil").exists()
    assert not (target_base / ".anvil.migrating").exists()
    assert _recursive_file_manifest(state) == {**before, ".state-operation.lock": b""}


@pytest.mark.skipif(os.name != "posix", reason="Independent POSIX flock observer")
def test_workspace_holds_source_custody_through_copy_and_publication(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import subprocess
    import sys

    from anvil.state.file_inspection import BoundedFileInspection

    workspace_module = importlib.import_module("anvil.cli.migrate_workspace")
    repo = tmp_path / "source-custody"
    repo.mkdir()
    state = _legacy(repo)
    token = state / ".state-operation.lock"
    token.touch()
    observation = """
import fcntl, json, sys
blocked = []
for name in sys.argv[1:]:
    with open(name, 'r+b') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            blocked.append(True)
        else:
            blocked.append(False)
print(json.dumps(blocked))
"""

    def locks_blocked():
        result = subprocess.run(
            [sys.executable, "-c", observation, str(token), str(state / "events.jsonl")],
            capture_output=True, check=True, timeout=10,
        )
        return json.loads(result.stdout)

    observed_phases = []
    copy_tree = BoundedFileInspection.copy_tree
    replace = workspace_module.os.replace

    def copy_under_custody(self, source, destination):
        assert locks_blocked() == [True, True]
        observed_phases.append("copy")
        return copy_tree(self, source, destination)

    def publish_under_custody(source, destination):
        assert locks_blocked() == [True, True]
        observed_phases.append("publication")
        return replace(source, destination)

    monkeypatch.setattr(BoundedFileInspection, "copy_tree", copy_under_custody)
    monkeypatch.setattr(workspace_module.os, "replace", publish_under_custody)
    result = _run(repo, "--yes")
    assert result.exit_code == 0, result.output
    assert observed_phases == ["copy", "publication"]
    assert locks_blocked() == [False, False]


def test_workspace_opaque_copy_does_not_create_missing_log_or_token(
    home: Path, tmp_path: Path,
) -> None:
    from tests.test_cli import _recursive_file_manifest

    repo = tmp_path / "no-log"
    repo.mkdir()
    state = _legacy(repo)
    (state / "events.jsonl").unlink()
    before = _recursive_file_manifest(state)
    result = _run(repo, "--yes")
    assert result.exit_code == 0, result.output
    target = Path(json.loads(result.stdout)["data"]["target"])
    assert _recursive_file_manifest(target) == before
    assert _recursive_file_manifest(state) == before


def test_workspace_existing_authority_skips_pending_legacy_recovery(
    home: Path, tmp_path: Path,
) -> None:
    from anvil.cli._helpers import _home_workspace_base
    from tests.test_cli import _recursive_file_manifest

    repo = tmp_path / "already-authoritative"
    repo.mkdir()
    source = _legacy(repo)
    (source / ".event-format-migration.json").write_bytes(b"pending original recovery")
    target = _home_workspace_base(repo) / ".anvil"
    target.mkdir(parents=True)
    (target / "state.db").write_bytes(b"existing authority")
    source_before = _recursive_file_manifest(source)
    target_before = _recursive_file_manifest(target)
    result = _run(repo, "--yes")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["status"] == "already_migrated"
    assert _recursive_file_manifest(source) == source_before
    assert _recursive_file_manifest(target) == target_before


def test_workspace_custody_refusal_preserves_unowned_staging(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    from contextlib import contextmanager

    from anvil.cli._helpers import _home_workspace_base
    from anvil.state.backend import SchemaProbeFailed
    from tests.test_cli import _recursive_file_manifest

    module = importlib.import_module("anvil.cli.migrate_workspace")
    repo = tmp_path / "custody-refusal"
    repo.mkdir()
    source = _legacy(repo)
    target_base = _home_workspace_base(repo)
    staging = target_base / ".anvil.migrating"
    staging.mkdir(parents=True)
    (staging / "previous-attempt").write_text("retain on admission refusal")
    source_before = _recursive_file_manifest(source)
    staging_before = _recursive_file_manifest(staging)

    @contextmanager
    def unavailable_custody(*args, **kwargs):
        raise SchemaProbeFailed("fixture coordination contention")
        yield  # pragma: no cover

    monkeypatch.setattr(module, "_state_operation_lock", unavailable_custody)
    result = _run(repo, "--yes")
    assert result.exit_code != 0
    assert _recursive_file_manifest(staging) == staging_before
    assert _recursive_file_manifest(source) == source_before
    assert not (target_base / ".anvil").exists()
