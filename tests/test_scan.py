"""Brownfield scan / ingest tests (backlog T008).

Covers the two surfaces of ``anvil scan`` / ``init --from-repo``:

* The pure scan engine (``scan.model`` + ``scan.prd_draft``): walking a tree,
  building + persisting a queryable codebase model, diffing a re-scan, and
  generating a draft PRD that the existing parser accepts.
* The CLI surface: ``scan`` seeds a non-empty draft PRD + ready tasks on the
  first run, a re-scan reports the file delta WITHOUT overwriting the seeded
  graph, the ``--json`` envelope is well-formed, and ``init --from-repo`` is
  the one-command convenience path.

Tests run in isolated tmp directories and never touch the real cwd state.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixture-repo builder
# ---------------------------------------------------------------------------


def _make_fixture_repo(root: Path) -> None:
    """Create a small, realistic multi-component working tree under *root*."""
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "core.py").write_text("def core():\n    return 1\n")
    (root / "src" / "pkg" / "util.py").write_text("def util():\n    return 2\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_core.py").write_text("def test_core():\n    pass\n")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("# Guide\n")
    (root / "README.md").write_text("# Fixture Project\n")


# ---------------------------------------------------------------------------
# Pure engine: scan.model
# ---------------------------------------------------------------------------


class TestScanModel:
    def test_scan_walks_tree_and_groups_by_component(self, tmp_path: Path) -> None:
        from anvil.scan.model import scan_working_tree

        _make_fixture_repo(tmp_path)
        model = scan_working_tree(tmp_path)

        paths = {f.path for f in model.files}
        assert "src/pkg/core.py" in paths
        assert "README.md" in paths
        assert model.file_count == 5

        components = model.components()
        assert set(components) == {"(root)", "src", "tests", "docs"}
        # Root-level README is bucketed under the synthetic "(root)" component.
        assert [f.path for f in components["(root)"]] == ["README.md"]

        langs = model.language_counts()
        assert langs["python"] == 3
        assert langs["markdown"] == 2

    def test_scan_excludes_noise_directories(self, tmp_path: Path) -> None:
        from anvil.scan.model import scan_working_tree

        _make_fixture_repo(tmp_path)
        # Noise dirs that must never appear in the model.
        (tmp_path / "node_modules" / "dep").mkdir(parents=True)
        (tmp_path / "node_modules" / "dep" / "index.js").write_text("x\n")
        (tmp_path / ".venv" / "lib").mkdir(parents=True)
        (tmp_path / ".venv" / "lib" / "junk.py").write_text("x\n")

        model = scan_working_tree(tmp_path)
        paths = {f.path for f in model.files}
        assert not any(p.startswith("node_modules/") for p in paths)
        assert not any(p.startswith(".venv/") for p in paths)

    def test_persist_and_load_roundtrip_is_queryable(self, tmp_path: Path) -> None:
        from anvil.scan.model import (
            load_model,
            save_model,
            scan_working_tree,
        )

        _make_fixture_repo(tmp_path)
        model = scan_working_tree(tmp_path)
        db_path = tmp_path / ".anvil" / "scan.db"
        save_model(model, db_path)

        # The persisted model must be a real, queryable SQLite row set.
        assert db_path.exists()
        conn = sqlite3.connect(str(db_path))
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM codebase_files"
            ).fetchone()
            assert count == model.file_count
            rows = conn.execute(
                "SELECT path, language FROM codebase_files "
                "WHERE component = 'src' ORDER BY path"
            ).fetchall()
        finally:
            conn.close()
        assert ("src/pkg/core.py", "python") in rows

        reloaded = load_model(db_path)
        assert reloaded is not None
        assert {f.path for f in reloaded.files} == {f.path for f in model.files}

    def test_load_missing_db_returns_none(self, tmp_path: Path) -> None:
        from anvil.scan.model import load_model

        assert load_model(tmp_path / "nope.db") is None

    def test_compute_delta_first_scan_is_all_added(self, tmp_path: Path) -> None:
        from anvil.scan.model import compute_delta, scan_working_tree

        _make_fixture_repo(tmp_path)
        model = scan_working_tree(tmp_path)
        delta = compute_delta(None, model)
        assert len(delta.added) == model.file_count
        assert delta.removed == []
        assert delta.changed == []
        assert delta.has_changes

    def test_compute_delta_reports_add_remove_change(self, tmp_path: Path) -> None:
        from anvil.scan.model import compute_delta, scan_working_tree

        _make_fixture_repo(tmp_path)
        before = scan_working_tree(tmp_path)

        (tmp_path / "src" / "pkg" / "core.py").write_text("def core():\n    return 99\n")
        (tmp_path / "src" / "pkg" / "new.py").write_text("def new():\n    return 3\n")
        (tmp_path / "docs" / "guide.md").unlink()

        after = scan_working_tree(tmp_path)
        delta = compute_delta(before, after)
        assert delta.added == ["src/pkg/new.py"]
        assert delta.removed == ["docs/guide.md"]
        assert delta.changed == ["src/pkg/core.py"]
        assert delta.has_changes

    def test_compute_delta_no_change(self, tmp_path: Path) -> None:
        from anvil.scan.model import compute_delta, scan_working_tree

        _make_fixture_repo(tmp_path)
        a = scan_working_tree(tmp_path)
        b = scan_working_tree(tmp_path)
        delta = compute_delta(a, b)
        assert not delta.has_changes
        assert len(delta.unchanged) == a.file_count


# ---------------------------------------------------------------------------
# Pure engine: scan.prd_draft
# ---------------------------------------------------------------------------


class TestPrdDraft:
    def test_draft_prd_parses_with_features_and_tasks(self, tmp_path: Path) -> None:
        from anvil.planning.template import parse_prd
        from anvil.scan.model import scan_working_tree
        from anvil.scan.prd_draft import draft_prd_from_model

        _make_fixture_repo(tmp_path)
        model = scan_working_tree(tmp_path)
        prd_text = draft_prd_from_model(model, project_name="Fixture Project")

        assert prd_text.strip()
        parsed = parse_prd(prd_text, prd_id="prd")
        # A draft anvil generates must always parse cleanly.
        assert parsed.errors == []
        assert len(parsed.features) >= 1
        assert len(parsed.tasks) >= 1
        # Every task has the acceptance-criteria + verification fields the
        # review gate requires (otherwise nothing reaches `ready`).
        for task in parsed.tasks:
            assert task.acceptance_criteria
            assert task.verification.commands

    def test_draft_prd_anchors_tasks_to_real_files(self, tmp_path: Path) -> None:
        from anvil.planning.template import parse_prd
        from anvil.scan.model import scan_working_tree
        from anvil.scan.prd_draft import draft_prd_from_model

        _make_fixture_repo(tmp_path)
        model = scan_working_tree(tmp_path)
        parsed = parse_prd(
            draft_prd_from_model(model, project_name="Fixture"), prd_id="prd"
        )
        all_likely = {f for t in parsed.tasks for f in t.likely_files}
        # Tasks must reference paths that actually exist in the scanned tree.
        assert "src/pkg/core.py" in all_likely or "src/pkg/util.py" in all_likely


# ---------------------------------------------------------------------------
# CLI surface: scan + init --from-repo
# ---------------------------------------------------------------------------


def _init(root: Path) -> None:
    original = os.getcwd()
    os.chdir(root)
    try:
        res = runner.invoke(app, ["init", "--name", "Scan Fixture"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
    finally:
        os.chdir(original)


class TestScanCommand:
    def test_scan_requires_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_fixture_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        res = runner.invoke(app, ["scan"], catch_exceptions=False)
        assert res.exit_code == 1
        assert "not initialized" in res.output.lower()

    def test_first_scan_seeds_prd_tasks_and_codebase_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)

        res = runner.invoke(app, ["scan"], catch_exceptions=False)
        assert res.exit_code == 0, res.output

        state_dir = tmp_path / ".anvil"
        # 1) a non-empty draft PRD was written
        prd_path = state_dir / "prd.md"
        assert prd_path.exists()
        assert prd_path.read_text(encoding="utf-8").strip()

        # 2) tasks were seeded and at least one is ready
        status = runner.invoke(app, ["status", "--json"], catch_exceptions=False)
        data = json.loads(status.output)["data"]
        assert data["tasks"]["total"] >= 1
        assert data["tasks"]["ready"] >= 1
        assert data["prd_status"] in {"approved", "reviewed"}

        # 3) a queryable codebase model row set was persisted
        scan_db = state_dir / "scan.db"
        assert scan_db.exists()
        conn = sqlite3.connect(str(scan_db))
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM codebase_files").fetchone()
        finally:
            conn.close()
        assert count == 5

    def test_rescan_reports_delta_without_overwriting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)

        first = runner.invoke(app, ["scan"], catch_exceptions=False)
        assert first.exit_code == 0, first.output

        # Capture the seeded task ids; a re-scan must not blow them away.
        before = json.loads(
            runner.invoke(app, ["list", "--json"], catch_exceptions=False).output
        )["data"]
        before_ids = {t["id"] for t in before["tasks"]}
        assert before_ids

        # Mutate the tree, then re-scan.
        (tmp_path / "src" / "pkg" / "core.py").write_text("def core():\n    return 99\n")
        (tmp_path / "src" / "pkg" / "added.py").write_text("def added():\n    return 4\n")
        (tmp_path / "docs" / "guide.md").unlink()

        res = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["ok"] is True
        assert payload["command"] == "scan"
        delta = payload["data"]["delta"]
        assert "src/pkg/added.py" in delta["added"]
        assert "docs/guide.md" in delta["removed"]
        assert "src/pkg/core.py" in delta["changed"]
        # Re-scan reported the delta rather than re-seeding.
        assert payload["data"]["seeded"] is None
        assert payload["data"]["first_scan"] is False

        # The seeded task graph is untouched.
        after = json.loads(
            runner.invoke(app, ["list", "--json"], catch_exceptions=False).output
        )["data"]
        after_ids = {t["id"] for t in after["tasks"]}
        assert after_ids == before_ids

    def test_scan_json_envelope_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)

        res = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["ok"] is True
        assert payload["command"] == "scan"
        data = payload["data"]
        for key in ("files_scanned", "components", "languages", "delta", "seeded"):
            assert key in data
        assert data["files_scanned"] == 5
        assert data["first_scan"] is True

    def test_rescan_does_not_reseed_when_only_named_prds_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project that holds only NON-default PRDs (no is_default row) and
        has no prd.md on disk is already populated — scan must NOT re-seed a
        fresh default draft graph on top of it.

        Regression for the brownfield re-seed suppressor: it probes list_prds()
        (any PRD exists), not bare get_prd() (default only). A named-only project
        returns None from get_prd(), so the old default-only check would slip
        through to `no_prd` and clobber the existing multi-PRD state.
        """
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        # Seed ONLY a named PRD row directly; no default PRD, no prd.md on disk.
        state_dir = tmp_path / ".anvil"
        assert not (state_dir / "prd.md").exists()
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            conn.execute(
                "INSERT INTO prds (id, project_id, status, is_default) "
                "VALUES ('v0.2', 'proj-1', 'approved', 0)"
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.chdir(tmp_path)

        res = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        # The existing named PRD suppresses re-seeding; no default draft written.
        assert payload["data"]["seeded"] is None
        assert not (state_dir / "prd.md").exists(), (
            "scan must not write a default prd.md over an existing named-PRD project"
        )

    def test_scan_force_reseeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)

        runner.invoke(app, ["scan"], catch_exceptions=False)
        # A plain re-scan does not re-seed.
        plain = json.loads(
            runner.invoke(app, ["scan", "--json"], catch_exceptions=False).output
        )
        assert plain["data"]["seeded"] is None
        # --force re-seeds the draft graph.
        forced = json.loads(
            runner.invoke(app, ["scan", "--json", "--force"], catch_exceptions=False).output
        )
        assert forced["data"]["seeded"] is not None
        assert forced["data"]["seeded"]["tasks"] >= 1


class TestInitFromRepo:
    def test_init_from_repo_scaffolds_and_seeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_fixture_repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        res = runner.invoke(app, ["init", "--from-repo"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert "Seeded draft project from repo" in res.output

        state_dir = tmp_path / ".anvil"
        assert (state_dir / "prd.md").exists()
        assert (state_dir / "scan.db").exists()

        status = json.loads(
            runner.invoke(app, ["status", "--json"], catch_exceptions=False).output
        )["data"]
        assert status["tasks"]["ready"] >= 1

    def test_from_repo_and_with_sample_mutually_exclusive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        res = runner.invoke(
            app, ["init", "--from-repo", "--with-sample"], catch_exceptions=False
        )
        assert res.exit_code == 1
        assert "mutually exclusive" in res.output.lower()
