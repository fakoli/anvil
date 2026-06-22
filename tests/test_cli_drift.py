"""Integration tests for `anvil drift` (backlog T026/B26, P1).

``drift`` is the read-only DRIFT VIEW over anvil's three-source
reconciliation: INTENT (plan/tasks) vs STATE (SQLite) vs FILESYSTEM/GIT
(expected files, agent branches, worktrees, packets). It reuses
``ReconciliationEngine.scan()`` in report mode (never ``fix()``), drops the
provider-dependent kinds, and works WITHOUT a configured sync provider.

Coverage groups:

* TestCleanProject       — a fresh init reports no drift (human + json, exit 0)
* TestMissingExpectedFile — a done task whose plan file is gone surfaces
* TestOrphanBranchDrift  — orphan agent branch surfaces (git-backed)
* TestStaleClaimDrift    — an expired-lease active claim surfaces
* TestProviderless       — drift never needs a configured provider
* TestStateRootEnv       — ANVIL_ROOT is honoured
* TestJsonEnvelope       — the v1.23.4 envelope shape
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, timedelta
from datetime import datetime as _datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from anvil.cli import app

runner = CliRunner()

_NOW = _datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_cli_sync.py seeding patterns)
# ---------------------------------------------------------------------------


def _init_project(root: Path, name: str = "DriftTest") -> None:
    """Run `anvil init` with cwd == root."""
    original = os.getcwd()
    os.chdir(root)
    try:
        r = runner.invoke(app, ["init", "--name", name], catch_exceptions=False)
        assert r.exit_code == 0, r.output
    finally:
        os.chdir(original)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """An initialized project root (the dir that contains .anvil/)."""
    _init_project(tmp_path)
    return tmp_path


def _open(root: Path, state_dir: Path | None = None) -> Any:
    from anvil.cli._helpers import _open_backend

    return _open_backend(state_dir if state_dir is not None else root / ".anvil")


def _seed_done_task(
    root: Path,
    *,
    task_id: str = "T001",
    feature_id: str = "F001",
    likely_files: list[str] | None = None,
    status: str = "done",
    state_dir: Path | None = None,
) -> None:
    """Seed a feature + task and walk it to a terminal status.

    ``likely_files`` are project-root-relative (e.g. ``src/widget.py``) — the
    intent declaration the missing_expected_file check compares against disk.
    Pass ``state_dir`` when the DB is not at ``root/.anvil`` (HOME-workspace
    layout, where state lives under ``~/.anvil/workspaces/<key>/.anvil``).
    """
    from anvil.state.models import EventDraft

    b = _open(root, state_dir)
    try:
        b.append(EventDraft(
            timestamp=_NOW, actor="test", action="feature.created",
            target_kind="feature", target_id=feature_id,
            payload_json={
                "id": feature_id, "title": "F", "description": "",
                "status": "proposed", "requirements": [], "tasks": [],
            },
        ))
        b.append(EventDraft(
            timestamp=_NOW, actor="test", action="task.created",
            target_kind="task", target_id=task_id,
            payload_json={
                "id": task_id, "feature_id": feature_id, "title": "Add widget",
                "description": "d", "status": "proposed", "priority": "medium",
                "dependencies": [], "conflict_groups": [], "scores": {},
                "acceptance_criteria": ["ok"], "implementation_notes": [],
                "verification": {
                    "commands": ["pytest"], "manual_steps": [],
                    "required_evidence": [],
                },
                "likely_files": likely_files or [],
                "parent_task_id": None,
                "created_at": _NOW.isoformat(), "updated_at": _NOW.isoformat(),
            },
        ))
        chain = [
            ("proposed", "drafted"), ("drafted", "reviewed"),
            ("reviewed", "ready"), ("ready", "claimed"),
            ("claimed", "in_progress"), ("in_progress", "needs_review"),
            ("needs_review", "accepted"), ("accepted", "done"),
        ]
        for frm, to in chain:
            if frm == status:
                break
            b.append(EventDraft(
                timestamp=_NOW, actor="test", action="task.status_changed",
                target_kind="task", target_id=task_id,
                payload_json={"task_id": task_id, "from": frm, "to": to},
            ))
            if to == status:
                break
    finally:
        b.close()


def _seed_ready_task(root: Path, *, task_id: str = "T001") -> None:
    """Seed a feature + task that stays in 'ready' (precursor for a claim)."""
    from anvil.state.models import EventDraft

    b = _open(root)
    try:
        b.append(EventDraft(
            timestamp=_NOW, actor="test", action="feature.created",
            target_kind="feature", target_id="F001",
            payload_json={
                "id": "F001", "title": "F", "description": "",
                "status": "proposed", "requirements": [], "tasks": [],
            },
        ))
        b.append(EventDraft(
            timestamp=_NOW, actor="test", action="task.created",
            target_kind="task", target_id=task_id,
            payload_json={
                "id": task_id, "feature_id": "F001", "title": "T",
                "description": "d", "status": "ready", "priority": "medium",
                "dependencies": [], "conflict_groups": [], "scores": {},
                "acceptance_criteria": ["ok"], "implementation_notes": [],
                "verification": {
                    "commands": ["pytest"], "manual_steps": [],
                    "required_evidence": [],
                },
                "likely_files": [], "parent_task_id": None,
                "created_at": _NOW.isoformat(), "updated_at": _NOW.isoformat(),
            },
        ))
    finally:
        b.close()


def _seed_stale_claim(root: Path, *, claim_id: str = "C001", task_id: str = "T001") -> None:
    """Insert an active claim whose lease already expired (stale)."""
    from anvil.state.models import EventDraft

    b = _open(root)
    try:
        b.append(EventDraft(
            timestamp=_NOW, actor="test", action="claim.created",
            target_kind="claim", target_id=claim_id,
            payload_json={
                "id": claim_id, "task_id": task_id, "claimed_by": "agent-x",
                "claim_type": "task", "status": "active", "branch": None,
                "worktree_path": None, "expected_files": [],
                "created_at": _NOW.isoformat(),
                # Lease expired two hours before _NOW -> stale.
                "lease_expires_at": (_NOW - timedelta(hours=2)).isoformat(),
                "last_heartbeat_at": _NOW.isoformat(),
            },
        ))
    finally:
        b.close()


def _git(cwd: Path, *args: str) -> None:
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr or r.stdout}")


def _init_git(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "commit", "--allow-empty", "-q", "-m", "init")


def _json_of(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# Clean project — no drift
# ---------------------------------------------------------------------------


class TestCleanProject:
    def test_clean_human_reports_no_drift(self, project: Path) -> None:
        r = runner.invoke(app, ["drift", "--cwd", str(project)], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        assert "No drift detected." in r.output

    def test_clean_json_is_empty_envelope(self, project: Path) -> None:
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(project)], catch_exceptions=False
        )
        assert r.exit_code == 0, r.output
        env = _json_of(r)
        assert env["ok"] is True
        assert env["command"] == "drift"
        assert env["data"]["drift"] == []
        assert env["data"]["summary"]["total"] == 0
        assert env["data"]["summary"]["by_category"] == {}

    def test_clean_with_existing_expected_file_no_drift(self, project: Path) -> None:
        """A done task whose plan file EXISTS on disk is not drift."""
        (project / "src").mkdir()
        (project / "src" / "widget.py").write_text("ok\n")
        _seed_done_task(project, likely_files=["src/widget.py"])
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(project)], catch_exceptions=False
        )
        env = _json_of(r)
        assert env["data"]["summary"]["total"] == 0, env

    def test_uninitialized_json_error_envelope(self, tmp_path: Path) -> None:
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(tmp_path)], catch_exceptions=False
        )
        assert r.exit_code != 0
        env = _json_of(r)
        assert env["ok"] is False
        assert env["command"] == "drift"
        assert env["error"]["code"] == "not_initialized"


# ---------------------------------------------------------------------------
# missing_expected_file — INTENT vs FILESYSTEM
# ---------------------------------------------------------------------------


class TestMissingExpectedFile:
    def test_done_task_missing_file_surfaces_human(self, project: Path) -> None:
        _seed_done_task(project, likely_files=["src/widget.py"])
        r = runner.invoke(app, ["drift", "--cwd", str(project)], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        assert "missing_expected_file" in r.output
        assert "T001" in r.output
        assert "src/widget.py" in r.output

    def test_done_task_missing_file_surfaces_json(self, project: Path) -> None:
        _seed_done_task(project, likely_files=["src/widget.py"])
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(project)], catch_exceptions=False
        )
        assert r.exit_code == 0, r.output
        env = _json_of(r)
        assert env["data"]["summary"]["total"] == 1
        assert env["data"]["summary"]["by_category"]["missing_expected_file"] == 1
        item = env["data"]["drift"][0]
        assert item["category"] == "missing_expected_file"
        assert item["task"] == "T001"
        assert item["file"] == "src/widget.py"
        assert item["severity"] == "warning"

    def test_non_terminal_task_missing_file_not_drift(self, project: Path) -> None:
        """A 'ready' task whose files don't exist yet is normal, not drift."""
        _seed_done_task(project, likely_files=["src/widget.py"], status="ready")
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(project)], catch_exceptions=False
        )
        env = _json_of(r)
        assert env["data"]["summary"]["total"] == 0, env


# ---------------------------------------------------------------------------
# orphan_branch — STATE vs GIT
# ---------------------------------------------------------------------------


class TestOrphanBranchDrift:
    def test_orphan_agent_branch_surfaces(self, tmp_path: Path) -> None:
        _init_project(tmp_path)
        _init_git(tmp_path)
        _git(tmp_path, "branch", "agent/t099-ghost")
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(tmp_path)], catch_exceptions=False
        )
        assert r.exit_code == 0, r.output
        env = _json_of(r)
        cats = {i["category"] for i in env["data"]["drift"]}
        assert "orphan_branch" in cats
        item = next(i for i in env["data"]["drift"] if i["category"] == "orphan_branch")
        assert item["branch"] == "agent/t099-ghost"


# ---------------------------------------------------------------------------
# stale_claim — STATE drift
# ---------------------------------------------------------------------------


class TestStaleClaimDrift:
    def test_stale_claim_surfaces(self, project: Path) -> None:
        _seed_ready_task(project, task_id="T001")
        _seed_stale_claim(project, claim_id="C001", task_id="T001")
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(project)], catch_exceptions=False
        )
        assert r.exit_code == 0, r.output
        env = _json_of(r)
        cats = {i["category"] for i in env["data"]["drift"]}
        assert "stale_claim" in cats
        item = next(i for i in env["data"]["drift"] if i["category"] == "stale_claim")
        assert item["task"] == "T001"
        assert item["severity"] == "error"


# ---------------------------------------------------------------------------
# Providerless — drift never requires a configured provider
# ---------------------------------------------------------------------------


class TestProviderless:
    def test_done_task_no_mapping_is_not_drift(self, project: Path) -> None:
        """Without the missing file, a done task with no SyncMapping must NOT
        surface as drift — that is a provider concern (`missing_sync_mapping`),
        which drift deliberately excludes. Proves drift works providerless."""
        (project / "src").mkdir()
        (project / "src" / "widget.py").write_text("ok\n")
        _seed_done_task(project, likely_files=["src/widget.py"])
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(project)], catch_exceptions=False
        )
        env = _json_of(r)
        cats = {i["category"] for i in env["data"]["drift"]}
        assert "missing_sync_mapping" not in cats
        assert "drift_sync_state" not in cats
        assert env["data"]["summary"]["total"] == 0


# ---------------------------------------------------------------------------
# ANVIL_ROOT
# ---------------------------------------------------------------------------


class TestStateRootEnv:
    def test_drift_honors_state_root_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ANVIL_ROOT -> project A; cwd -> elsewhere; drift reads A."""
        proj = tmp_path / "proj"
        elsewhere = tmp_path / "elsewhere"
        proj.mkdir()
        elsewhere.mkdir()
        _init_project(proj)
        _seed_done_task(proj, likely_files=["src/widget.py"])

        monkeypatch.setenv("ANVIL_ROOT", str(proj))
        monkeypatch.chdir(elsewhere)

        # No --cwd: resolution must fall through to ANVIL_ROOT.
        r = runner.invoke(app, ["drift", "--json"], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        env = _json_of(r)
        assert env["ok"] is True
        assert env["data"]["summary"]["total"] == 1
        assert env["data"]["drift"][0]["category"] == "missing_expected_file"

    def test_drift_state_root_missing_dir_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ANVIL_ROOT pointing at a dir with no .anvil/ fails loud."""
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("ANVIL_ROOT", str(empty))
        monkeypatch.chdir(tmp_path)
        r = runner.invoke(app, ["drift"], catch_exceptions=False)
        assert r.exit_code != 0


# ---------------------------------------------------------------------------
# JSON envelope shape
# ---------------------------------------------------------------------------


class TestJsonEnvelope:
    def test_envelope_top_level_keys(self, project: Path) -> None:
        _seed_done_task(project, likely_files=["a.py", "b.py"])
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(project)], catch_exceptions=False
        )
        env = _json_of(r)
        assert set(env.keys()) == {"ok", "command", "data"}
        assert set(env["data"].keys()) == {"drift", "summary"}
        assert set(env["data"]["summary"].keys()) == {"total", "by_category"}
        # Two missing files on one done task -> two drift items.
        assert env["data"]["summary"]["total"] == 2
        for item in env["data"]["drift"]:
            assert set(item.keys()) == {
                "category", "severity", "description",
                "task", "file", "path", "branch", "target_kind", "target_id",
            }

    def test_single_json_line_on_stdout(self, project: Path) -> None:
        """--json emits exactly one parseable line (pipe-safe contract)."""
        _seed_done_task(project, likely_files=["src/widget.py"])
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(project)], catch_exceptions=False
        )
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, r.stdout
        json.loads(lines[0])


# ---------------------------------------------------------------------------
# orphan_packet — MUST-FIX 2 (was UNREACHABLE through drift/sync)
# ---------------------------------------------------------------------------


class TestOrphanPacketDrift:
    """A `.anvil/packets/<TASK>.md` with no matching task surfaces.

    MUST-FIX 2: the engine's hardcoded ``state_dir / ".anvil" /
    "packets"`` never matched the CLI, which passes ``state_dir`` AS the
    ``.anvil/`` directory — so it looked at
    ``.anvil/.anvil/packets`` and ``orphan_packet`` never fired.
    These CLI-level tests prove it now resolves to ``.anvil/packets``.
    """

    def test_orphan_packet_surfaces_human(self, project: Path) -> None:
        pkt = project / ".anvil" / "packets" / "TZZZ.md"
        pkt.parent.mkdir(parents=True, exist_ok=True)
        pkt.write_text("# orphan work packet\n")
        r = runner.invoke(app, ["drift", "--cwd", str(project)], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        assert "orphan_packet" in r.output
        assert "TZZZ" in r.output

    def test_orphan_packet_surfaces_json(self, project: Path) -> None:
        pkt = project / ".anvil" / "packets" / "TZZZ.md"
        pkt.parent.mkdir(parents=True, exist_ok=True)
        pkt.write_text("# orphan work packet\n")
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(project)], catch_exceptions=False
        )
        assert r.exit_code == 0, r.output
        env = _json_of(r)
        cats = {i["category"] for i in env["data"]["drift"]}
        assert "orphan_packet" in cats, env
        item = next(i for i in env["data"]["drift"] if i["category"] == "orphan_packet")
        assert item["task"] == "TZZZ"
        # The packet FILE path is carried in ``path`` (a real .md file).
        assert item["path"] is not None
        assert item["path"].endswith("TZZZ.md")

    def test_packet_with_matching_task_not_orphan(self, project: Path) -> None:
        """A packet whose task EXISTS in state is not an orphan."""
        _seed_done_task(project, task_id="T001", likely_files=[])
        pkt = project / ".anvil" / "packets" / "T001.md"
        pkt.parent.mkdir(parents=True, exist_ok=True)
        pkt.write_text("# real packet\n")
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(project)], catch_exceptions=False
        )
        env = _json_of(r)
        cats = {i["category"] for i in env["data"]["drift"]}
        assert "orphan_packet" not in cats, env


# ---------------------------------------------------------------------------
# MUST-FIX 3 — --json pipe-safe on invalid ANVIL_ROOT
# ---------------------------------------------------------------------------


class TestBadStateRootJson:
    def test_bad_state_root_json_is_error_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ANVIL_ROOT pointing at a dir with no .anvil/ + --json
        must emit a parseable {"ok": false, ...} envelope, not a raw
        ClickException, and exit non-zero."""
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("ANVIL_ROOT", str(empty))
        monkeypatch.chdir(tmp_path)
        # No --cwd: resolution falls through to ANVIL_ROOT and raises.
        r = runner.invoke(app, ["drift", "--json"], catch_exceptions=False)
        assert r.exit_code != 0
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, r.stdout
        env = json.loads(lines[0])
        assert env["ok"] is False
        assert env["command"] == "drift"
        assert env["error"]["code"] == "state_root_invalid"

    def test_bad_state_root_human_still_clickexception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --json the clean human ClickException path is unchanged."""
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("ANVIL_ROOT", str(empty))
        monkeypatch.chdir(tmp_path)
        r = runner.invoke(app, ["drift"], catch_exceptions=False)
        assert r.exit_code != 0
        # stdout is NOT a JSON envelope on the human path.
        assert not r.stdout.strip().startswith("{")


# ---------------------------------------------------------------------------
# SHOULD-FIX — orphan_worktree directory is NOT mislabelled as a `file`
# ---------------------------------------------------------------------------


class TestOrphanWorktreeFileLabel:
    def test_worktree_dir_is_path_not_file(self, tmp_path: Path) -> None:
        """An orphan worktree's directory must land in ``path``, with
        ``file`` left null (it is a directory, not a plan file)."""
        _init_project(tmp_path)
        _init_git(tmp_path)
        _git(tmp_path, "branch", "agent/t099-ghost")
        wt_path = tmp_path.parent / "wt-t099-ghost"
        _git(tmp_path, "worktree", "add", str(wt_path), "agent/t099-ghost")
        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(tmp_path)], catch_exceptions=False
        )
        assert r.exit_code == 0, r.output
        env = _json_of(r)
        wt_items = [i for i in env["data"]["drift"]
                    if i["category"] == "orphan_worktree"]
        assert wt_items, env
        item = wt_items[0]
        assert item["file"] is None, item
        assert item["path"] is not None and item["path"].endswith("wt-t099-ghost")


# ---------------------------------------------------------------------------
# TestHomeWorkspaceLayout — the CLI must resolve likely_files against the real
# CHECKOUT, not the shared workspace state dir. This is the ONLY drift test that
# runs the default (workspace) layout, so it is what pins the four CLI call
# sites that thread `project_root` (drift / sync x2 / doctor). Under the
# suite-wide `ANVIL_STATE_LAYOUT=local` default, checkout == state_dir.parent, so
# the wiring is a no-op and reverting it leaves every other test green.
# ---------------------------------------------------------------------------


class TestHomeWorkspaceLayout:
    def test_drift_resolves_files_against_checkout_not_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from anvil.cli._helpers import _resolve_state_dir

        # Force the production HOME-workspace layout, with HOME redirected into
        # tmp so the shared workspace lands under tmp, never the real ~/.anvil.
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
        monkeypatch.setenv("HOME", str(home))

        # The real checkout lives elsewhere and DOES contain the expected file.
        checkout = tmp_path / "checkout"
        (checkout / "src").mkdir(parents=True)
        (checkout / "src" / "widget.py").write_text("ok\n")

        _init_project(checkout)

        # State landed in the shared workspace, NOT under the checkout — assert
        # the split is real, else this test would prove nothing.
        state_dir = _resolve_state_dir(checkout)
        assert home in state_dir.parents, state_dir
        assert checkout not in state_dir.parents, state_dir

        _seed_done_task(
            checkout, likely_files=["src/widget.py"], state_dir=state_dir,
        )

        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(checkout)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        env = _json_of(r)
        # The file exists in the checkout -> no missing_expected_file. If the CLI
        # reverted to deriving the root from state_dir it would probe the
        # workspace base and false-flag this file.
        missing = [i for i in env["data"]["drift"]
                   if i["category"] == "missing_expected_file"]
        assert missing == [], env

    def test_drift_handles_repo_root_docs_and_bin_src_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
        monkeypatch.setenv("HOME", str(home))

        checkout = tmp_path / "checkout"
        (checkout / "bin" / "src" / "anvil").mkdir(parents=True)
        (checkout / "bin" / "pyproject.toml").write_text("[project]\nname='x'\n")
        (checkout / "bin" / "src" / "anvil" / "widget.py").write_text("ok\n")
        (checkout / "docs").mkdir()
        (checkout / "docs" / "design.md").write_text("ok\n")
        (checkout / "tests").mkdir()
        (checkout / "tests" / "test_widget.py").write_text("ok\n")

        _init_project(checkout)

        from anvil.cli._helpers import _resolve_state_dir

        state_dir = _resolve_state_dir(checkout)
        _seed_done_task(
            checkout,
            likely_files=[
                "src/anvil/widget.py",
                "docs/design.md",
                "tests/test_widget.py",
            ],
            state_dir=state_dir,
        )

        r = runner.invoke(
            app, ["drift", "--json", "--cwd", str(checkout)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0, r.output
        env = _json_of(r)
        missing = [i for i in env["data"]["drift"]
                   if i["category"] == "missing_expected_file"]
        assert missing == [], env
