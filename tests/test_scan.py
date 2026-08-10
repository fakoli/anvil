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

import importlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

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
            (count,) = conn.execute("SELECT COUNT(*) FROM codebase_files").fetchone()
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

        (tmp_path / "src" / "pkg" / "core.py").write_text(
            "def core():\n    return 99\n"
        )
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
        res = runner.invoke(
            app, ["init", "--name", "Scan Fixture"], catch_exceptions=False
        )
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

    @pytest.mark.parametrize(
        "failure,message",
        [
            ("loader", "Windows path API unavailable (library load failed)"),
            ("mapping", "Windows path case mapping failed"),
            ("comparison", "Windows path comparison failed"),
        ],
    )
    @pytest.mark.parametrize("json_output", [False, True], ids=["human", "json"])
    def test_scan_native_path_failure_is_bounded_and_seed_atomic(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: str,
        message: str,
        json_output: bool,
    ) -> None:
        from anvil.planning import inference as inference_module
        from anvil.planning.inference import PathIdentityError

        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        events_path = tmp_path / ".anvil" / "events.jsonl"
        before = events_path.read_bytes()
        state_before = (tmp_path / ".anvil" / "state.db").read_bytes()
        monkeypatch.setattr(
            inference_module, "_uses_windows_path_identity", lambda: True
        )
        def fail_identity(path: Path) -> None:
            raise PathIdentityError(message)

        monkeypatch.setattr(
            inference_module,
            "_windows_existing_path_identity",
            fail_identity,
        )

        arguments = ["scan"] + (["--json"] if json_output else [])
        result = runner.invoke(app, arguments, catch_exceptions=False)

        assert result.exit_code == 1
        bounded = f"seed planning inference refused: {message}"
        if json_output:
            payload = json.loads(result.output)
            assert payload["error"] == {
                "code": "path_identity_error",
                "message": bounded,
            }
        else:
            assert result.output == f"Error: {bounded}\n"
        assert events_path.read_bytes() == before
        assert (tmp_path / ".anvil" / "state.db").read_bytes() == state_before
        assert not (tmp_path / ".anvil" / "scan.db").exists()
        assert not (tmp_path / ".anvil" / "prd.md").exists()
        connection = sqlite3.connect(tmp_path / ".anvil" / "state.db")
        try:
            assert connection.execute("SELECT COUNT(*) FROM prds").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM features").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        finally:
            connection.close()

    @pytest.mark.parametrize("json_output", [False, True], ids=["human", "json"])
    def test_scan_oversized_path_failure_restores_artifacts_and_retry_seeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        json_output: bool,
    ) -> None:
        from anvil.planning import inference as inference_module
        from anvil.planning import template as template_module

        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        oversized = "é" * 2_048 + "a"
        original_parse = template_module.parse_prd
        parse_calls = 0

        def fail_first_parse(*args: object, **kwargs: object) -> object:
            nonlocal parse_calls
            result = original_parse(*args, **kwargs)
            parse_calls += 1
            if parse_calls == 1:
                result.tasks[0].likely_files.append(oversized)
            return result

        monkeypatch.setattr(template_module, "parse_prd", fail_first_parse)
        inference_module._cached_windows_path_key.cache_clear()
        inference_module._cached_windows_paths_equal.cache_clear()
        key_before = inference_module._cached_windows_path_key.cache_info()
        comparison_before = (
            inference_module._cached_windows_paths_equal.cache_info()
        )
        state_dir = tmp_path / ".anvil"
        events_path = state_dir / "events.jsonl"
        events_before = events_path.read_bytes()
        state_before = (state_dir / "state.db").read_bytes()

        arguments = ["scan"] + (["--json"] if json_output else [])
        failed = runner.invoke(app, arguments, catch_exceptions=False)

        message = (
            "seed planning inference refused: bundle planning requires "
            "likely-file paths no longer than 4096 UTF-8 bytes"
        )
        assert failed.exit_code == 1
        if json_output:
            assert json.loads(failed.output)["error"] == {
                "code": "path_identity_error",
                "message": message,
            }
        else:
            assert failed.output == f"Error: {message}\n"
        assert len(message) <= 4_096
        assert message.encode("cp1252")
        assert events_path.read_bytes() == events_before
        assert (state_dir / "state.db").read_bytes() == state_before
        assert not (state_dir / "scan.db").exists()
        assert not (state_dir / "prd.md").exists()
        assert inference_module._cached_windows_path_key.cache_info() == key_before
        assert (
            inference_module._cached_windows_paths_equal.cache_info()
            == comparison_before
        )

        retried = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)

        assert retried.exit_code == 0, retried.output
        retry_data = json.loads(retried.output)["data"]
        assert retry_data["first_scan"] is True
        assert retry_data["seeded"] is not None
        assert retry_data["seeded"]["ready"] >= 1
        assert (state_dir / "scan.db").exists()
        assert (state_dir / "prd.md").exists()

    def test_force_scan_failure_restores_existing_artifacts_byte_for_byte(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from anvil.planning import inference as inference_module
        from anvil.planning import template as template_module

        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        seeded = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)
        assert seeded.exit_code == 0, seeded.output
        state_dir = tmp_path / ".anvil"
        scan_before = (state_dir / "scan.db").read_bytes()
        prd_before = (state_dir / "prd.md").read_bytes()
        events_before = (state_dir / "events.jsonl").read_bytes()
        connection = sqlite3.connect(state_dir / "state.db")
        try:
            counts_before = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("prds", "features", "tasks", "conflict_groups")
            )
        finally:
            connection.close()
        state_before = (state_dir / "state.db").read_bytes()

        original_parse = template_module.parse_prd

        def parse_with_oversized_path(*args: object, **kwargs: object) -> object:
            result = original_parse(*args, **kwargs)
            result.tasks[0].likely_files.append("é" * 2_048 + "a")
            return result

        monkeypatch.setattr(
            template_module, "parse_prd", parse_with_oversized_path
        )
        inference_module._cached_windows_path_key.cache_clear()
        inference_module._cached_windows_paths_equal.cache_clear()
        key_before = inference_module._cached_windows_path_key.cache_info()
        comparison_before = (
            inference_module._cached_windows_paths_equal.cache_info()
        )

        failed = runner.invoke(
            app, ["scan", "--force", "--json"], catch_exceptions=False
        )

        assert failed.exit_code == 1
        assert json.loads(failed.output)["error"]["code"] == "path_identity_error"
        assert (state_dir / "scan.db").read_bytes() == scan_before
        assert (state_dir / "prd.md").read_bytes() == prd_before
        assert (state_dir / "events.jsonl").read_bytes() == events_before
        assert (state_dir / "state.db").read_bytes() == state_before
        assert inference_module._cached_windows_path_key.cache_info() == key_before
        assert (
            inference_module._cached_windows_paths_equal.cache_info()
            == comparison_before
        )
        connection = sqlite3.connect(state_dir / "state.db")
        try:
            counts_after = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("prds", "features", "tasks", "conflict_groups")
            )
        finally:
            connection.close()
        assert counts_after == counts_before

    @pytest.mark.parametrize("mode", ["first-run", "force"])
    @pytest.mark.parametrize("failure_point", ["save-model", "prd-write"])
    def test_every_post_mutation_failure_rolls_back_and_retry_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        failure_point: str,
    ) -> None:
        scan_module = importlib.import_module("anvil.cli.scan")
        scan_model = importlib.import_module("anvil.scan.model")

        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        state_dir = tmp_path / ".anvil"
        if mode == "force":
            seeded = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)
            assert seeded.exit_code == 0, seeded.output

        artifacts_before = {
            name: (state_dir / name).read_bytes()
            if (state_dir / name).exists()
            else None
            for name in ("scan.db", "prd.md")
        }
        events_before = (state_dir / "events.jsonl").read_bytes()
        state_before = (state_dir / "state.db").read_bytes()

        with monkeypatch.context() as injected:
            if failure_point == "save-model":
                original_save = scan_model.save_model

                def fail_after_save(*args: object, **kwargs: object) -> None:
                    original_save(*args, **kwargs)
                    raise OSError("injected raw save failure")

                injected.setattr(scan_model, "save_model", fail_after_save)
            else:
                original_write = scan_module._write_generated_prd

                def fail_after_prd_write(path: Path, text: str) -> None:
                    original_write(path, text)
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write("\n# injected mutation\n")
                    raise OSError("injected raw PRD failure")

                injected.setattr(
                    scan_module, "_write_generated_prd", fail_after_prd_write
                )

            arguments = ["scan", "--json"]
            if mode == "force":
                arguments.append("--force")
            failed = runner.invoke(app, arguments, catch_exceptions=False)

        assert failed.exit_code == 1
        assert json.loads(failed.output)["error"] == {
            "code": "scan_artifact_error",
            "message": "scan artifact update failed; prior artifacts were restored",
        }
        assert "injected" not in failed.output
        assert str(tmp_path) not in failed.output
        assert (state_dir / "events.jsonl").read_bytes() == events_before
        assert (state_dir / "state.db").read_bytes() == state_before
        for name, prior_bytes in artifacts_before.items():
            artifact = state_dir / name
            if prior_bytes is None:
                assert not artifact.exists()
            else:
                assert artifact.read_bytes() == prior_bytes
        recovery_parent = state_dir / "recovery"
        assert not list(recovery_parent.glob("scan-*"))

        retried = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)
        assert retried.exit_code == 0, retried.output
        retry_data = json.loads(retried.output)["data"]
        if mode == "first-run":
            assert retry_data["first_scan"] is True
            assert retry_data["seeded"] is not None
        else:
            assert retry_data["seeded"] is None

    @pytest.mark.parametrize("mode", ["first-run", "force"])
    @pytest.mark.parametrize("restore_failure", ["scan.db", "prd.md"])
    def test_incomplete_restore_retains_durable_backups_and_retry_recovers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        restore_failure: str,
    ) -> None:
        scan_module = importlib.import_module("anvil.cli.scan")

        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        state_dir = tmp_path / ".anvil"
        if mode == "force":
            seeded = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)
            assert seeded.exit_code == 0, seeded.output

        artifacts_before = {
            name: (state_dir / name).read_bytes()
            if (state_dir / name).exists()
            else None
            for name in ("scan.db", "prd.md")
        }
        events_before = (state_dir / "events.jsonl").read_bytes()
        state_before = (state_dir / "state.db").read_bytes()
        restore_attempts: list[str] = []

        with monkeypatch.context() as injected:
            original_write = scan_module._write_generated_prd
            original_restore = scan_module._restore_scan_artifact

            def fail_after_prd_write(path: Path, text: str) -> None:
                original_write(path, text)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("\n# injected mutation\n")
                raise OSError("injected raw PRD failure")

            def fail_one_restore(
                recovery_state_dir: Path,
                recovery_root: Path,
                artifact_name: str,
            ) -> None:
                restore_attempts.append(artifact_name)
                if artifact_name == restore_failure:
                    raise OSError("injected raw restore failure")
                original_restore(
                    recovery_state_dir, recovery_root, artifact_name
                )

            injected.setattr(
                scan_module, "_write_generated_prd", fail_after_prd_write
            )
            injected.setattr(
                scan_module, "_restore_scan_artifact", fail_one_restore
            )
            arguments = ["scan", "--json"]
            if mode == "force":
                arguments.append("--force")
            failed = runner.invoke(app, arguments, catch_exceptions=False)

        assert restore_attempts == ["scan.db", "prd.md"]
        assert failed.exit_code == 1
        error = json.loads(failed.output)["error"]
        assert error["code"] == "scan_recovery_incomplete"
        assert error["message"].startswith(
            "scan artifact recovery incomplete; backups retained at "
            "recovery/scan-"
        )
        assert len(error["message"]) <= 4_096
        assert error["message"].encode("cp1252")
        assert "injected" not in error["message"]
        assert str(tmp_path) not in error["message"]
        token = error["message"].rsplit("/", maxsplit=1)[1]
        assert len(token) == len("scan-") + 16
        recovery_root = state_dir / "recovery" / token
        assert recovery_root.is_dir()
        for name, prior_bytes in artifacts_before.items():
            marker = recovery_root / f"{name}.absent"
            backup = recovery_root / f"{name}.backup"
            if prior_bytes is None:
                assert marker.read_bytes() == b"absent\n"
                assert not backup.exists()
            else:
                assert backup.read_bytes() == prior_bytes
                assert not marker.exists()
        assert (state_dir / "events.jsonl").read_bytes() == events_before
        assert (state_dir / "state.db").read_bytes() == state_before

        original_resume = scan_module._resume_scan_recovery
        restored_snapshot: dict[str, bytes | None] = {}
        resume_calls = 0

        def observe_automatic_resume(recovery_state_dir: Path) -> None:
            nonlocal resume_calls
            original_resume(recovery_state_dir)
            resume_calls += 1
            for name in ("scan.db", "prd.md"):
                artifact = recovery_state_dir / name
                restored_snapshot[name] = (
                    artifact.read_bytes() if artifact.exists() else None
                )

        with monkeypatch.context() as retry_patch:
            retry_patch.setattr(
                scan_module, "_resume_scan_recovery", observe_automatic_resume
            )
            retried = runner.invoke(
                app, ["scan", "--json"], catch_exceptions=False
            )

        assert resume_calls == 1
        assert restored_snapshot == artifacts_before
        assert not recovery_root.exists()
        assert retried.exit_code == 0, retried.output
        retry_data = json.loads(retried.output)["data"]
        if mode == "first-run":
            assert retry_data["first_scan"] is True
            assert retry_data["seeded"] is not None
        else:
            assert retry_data["seeded"] is None

    def test_plain_rescan_write_failure_restores_scan_artifacts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_model = importlib.import_module("anvil.scan.model")
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        first = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)
        assert first.exit_code == 0, first.output
        state_dir = tmp_path / ".anvil"
        artifacts_before = {
            name: (state_dir / name).read_bytes()
            for name in ("scan.db", "prd.md")
        }
        (tmp_path / "README.md").write_text("# Changed\n", encoding="utf-8")
        original_save = scan_model.save_model

        def fail_after_save(*args: object, **kwargs: object) -> None:
            original_save(*args, **kwargs)
            raise OSError("injected ordinary rescan failure")

        monkeypatch.setattr(scan_model, "save_model", fail_after_save)
        failed = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)

        assert failed.exit_code == 1
        assert json.loads(failed.output)["error"]["code"] == "scan_artifact_error"
        assert "injected" not in failed.output
        assert {
            name: (state_dir / name).read_bytes()
            for name in ("scan.db", "prd.md")
        } == artifacts_before

    @pytest.mark.parametrize("unsafe_target", ["state-artifact", "recovery-parent"])
    def test_recovery_refuses_non_regular_paths_before_mutation(
        self,
        tmp_path: Path,
        unsafe_target: str,
    ) -> None:
        scan_module = importlib.import_module("anvil.cli.scan")
        state_dir = tmp_path / ".anvil"
        state_dir.mkdir()
        (state_dir / "scan.db").write_bytes(b"scan-before")
        (state_dir / "prd.md").write_bytes(b"prd-before")
        if unsafe_target == "state-artifact":
            (state_dir / "prd.md").unlink()
            (state_dir / "prd.md").mkdir()
        else:
            (state_dir / "recovery").write_bytes(b"not-a-directory")

        with pytest.raises(scan_module.ScanArtifactError):
            scan_module._create_scan_recovery(state_dir)

        assert (state_dir / "scan.db").read_bytes() == b"scan-before"
        if unsafe_target == "state-artifact":
            assert (state_dir / "prd.md").is_dir()
        else:
            assert (state_dir / "prd.md").read_bytes() == b"prd-before"
            assert (state_dir / "recovery").read_bytes() == b"not-a-directory"

    def test_recovery_refuses_symlink_backup_without_touching_target(
        self, tmp_path: Path
    ) -> None:
        scan_module = importlib.import_module("anvil.cli.scan")
        state_dir = tmp_path / ".anvil"
        state_dir.mkdir()
        (state_dir / "scan.db").write_bytes(b"scan-before")
        (state_dir / "prd.md").write_bytes(b"prd-before")
        token, recovery_root = scan_module._create_scan_recovery(state_dir)
        external = tmp_path / "external-secret"
        external.write_bytes(b"do-not-touch")
        backup = recovery_root / "prd.md.backup"
        backup.unlink()
        try:
            backup.symlink_to(external)
        except OSError:
            pytest.skip("symlink creation is unavailable on this host")

        with pytest.raises(scan_module.ScanRecoveryError) as raised:
            scan_module._resume_scan_recovery(state_dir)

        assert raised.value.token == token
        assert external.read_bytes() == b"do-not-touch"
        assert (state_dir / "scan.db").read_bytes() == b"scan-before"
        assert (state_dir / "prd.md").read_bytes() == b"prd-before"

    def test_backup_creation_failure_is_bounded_and_leaves_no_active_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_module = importlib.import_module("anvil.cli.scan")
        state_dir = tmp_path / ".anvil"
        state_dir.mkdir()
        (state_dir / "scan.db").write_bytes(b"scan-before")
        (state_dir / "prd.md").write_bytes(b"prd-before")
        original_copy = scan_module._copy_regular_exclusive

        def fail_prd_copy(source: Path, destination: Path) -> None:
            if source.name == "prd.md":
                raise OSError("injected backup failure")
            original_copy(source, destination)

        monkeypatch.setattr(
            scan_module, "_copy_regular_exclusive", fail_prd_copy
        )
        with pytest.raises(scan_module.ScanArtifactError) as raised:
            scan_module._create_scan_recovery(state_dir)

        assert "injected" not in str(raised.value)
        recovery_parent = state_dir / "recovery"
        assert not list(recovery_parent.iterdir())
        assert (state_dir / "scan.db").read_bytes() == b"scan-before"
        assert (state_dir / "prd.md").read_bytes() == b"prd-before"

    def test_failed_restore_verification_retains_record_for_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_module = importlib.import_module("anvil.cli.scan")
        state_dir = tmp_path / ".anvil"
        state_dir.mkdir()
        (state_dir / "scan.db").write_bytes(b"scan-before")
        (state_dir / "prd.md").write_bytes(b"prd-before")
        _, recovery_root = scan_module._create_scan_recovery(state_dir)
        (state_dir / "scan.db").write_bytes(b"scan-after")
        (state_dir / "prd.md").write_bytes(b"prd-after")

        monkeypatch.setattr(
            scan_module,
            "_verify_restored_scan_artifacts",
            lambda *_args: False,
        )
        assert not scan_module._restore_scan_recovery(state_dir, recovery_root)
        assert recovery_root.is_dir()

        monkeypatch.undo()
        assert scan_module._restore_scan_recovery(state_dir, recovery_root)
        assert (state_dir / "scan.db").read_bytes() == b"scan-before"
        assert (state_dir / "prd.md").read_bytes() == b"prd-before"
        assert not recovery_root.exists()

    def test_retirement_cleanup_failure_retries_without_reactivation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_module = importlib.import_module("anvil.cli.scan")
        state_dir = tmp_path / ".anvil"
        state_dir.mkdir()
        (state_dir / "scan.db").write_bytes(b"scan-before")
        (state_dir / "prd.md").write_bytes(b"prd-before")
        _, recovery_root = scan_module._create_scan_recovery(state_dir)
        original_rmtree = scan_module.shutil.rmtree

        def fail_retired_cleanup(path: Path) -> None:
            if path.name.startswith(".retired-scan-"):
                raise OSError("injected retirement failure")
            original_rmtree(path)

        monkeypatch.setattr(scan_module.shutil, "rmtree", fail_retired_cleanup)
        assert not scan_module._retire_scan_recovery(recovery_root)
        retired = list((state_dir / "recovery").glob(".retired-scan-*"))
        assert len(retired) == 1
        assert not recovery_root.exists()

        monkeypatch.undo()
        scan_module._resume_scan_recovery(state_dir)
        assert not retired[0].exists()
        assert (state_dir / "scan.db").read_bytes() == b"scan-before"
        assert (state_dir / "prd.md").read_bytes() == b"prd-before"

    def test_concurrent_recovery_creation_leaves_one_resumable_record(
        self, tmp_path: Path
    ) -> None:
        scan_module = importlib.import_module("anvil.cli.scan")
        state_dir = tmp_path / ".anvil"
        state_dir.mkdir()
        (state_dir / "scan.db").write_bytes(b"scan-before")
        (state_dir / "prd.md").write_bytes(b"prd-before")
        barrier = threading.Barrier(2)
        outcome_lock = threading.Lock()
        created: list[Path] = []
        errors: list[BaseException] = []

        def create() -> None:
            barrier.wait(timeout=5)
            try:
                _token, recovery_root = scan_module._create_scan_recovery(state_dir)
                with outcome_lock:
                    created.append(recovery_root)
            except BaseException as exc:
                with outcome_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not any(thread.is_alive() for thread in threads)
        assert len(created) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], scan_module.ScanRecoveryError)
        active = list((state_dir / "recovery").glob("scan-*"))
        assert active == created

        scan_module._resume_scan_recovery(state_dir)
        assert not active[0].exists()
        assert (state_dir / "scan.db").read_bytes() == b"scan-before"
        assert (state_dir / "prd.md").read_bytes() == b"prd-before"

    def test_cross_process_scan_recovery_session_refuses_second_writer(
        self, tmp_path: Path
    ) -> None:
        import subprocess
        import sys

        scan_module = importlib.import_module("anvil.cli.scan")
        state_dir = tmp_path / ".anvil"
        state_dir.mkdir()
        probe = (
            "import importlib, pathlib, sys\n"
            "scan = importlib.import_module('anvil.cli.scan')\n"
            "scan._SCAN_LOCK_TIMEOUT_SECONDS = 0.1\n"
            "try:\n"
            "    with scan._exclusive_scan_session(pathlib.Path(sys.argv[1])):\n"
            "        print('acquired')\n"
            "except scan.ScanLockedError:\n"
            "    print('locked')\n"
        )

        with scan_module._exclusive_scan_session(state_dir):
            completed = subprocess.run(  # noqa: S603
                [sys.executable, "-c", probe, str(state_dir)],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )

        assert completed.stdout.strip() == "locked"

    def test_successful_seed_retirement_failure_preserves_committed_artifacts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_module = importlib.import_module("anvil.cli.scan")
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        state_dir = tmp_path / ".anvil"
        with monkeypatch.context() as injected:
            injected.setattr(
                scan_module,
                "_retire_scan_recovery",
                lambda *_args, **_kwargs: False,
            )
            with pytest.raises(scan_module.ScanRecoveryError):
                scan_module.run_scan_and_report(state_dir, tmp_path)

        prd_bytes = (state_dir / "prd.md").read_bytes()
        scan_bytes = (state_dir / "scan.db").read_bytes()
        active = list((state_dir / "recovery").glob("scan-*"))
        assert len(active) == 1
        assert (active[0] / "committed").read_bytes() == b"committed\n"
        backend = scan_module._open_backend(state_dir)
        try:
            assert len(backend.list_prds()) == 1
            assert len(backend.list_tasks()) == 4
        finally:
            backend.close()

        scan_module._resume_scan_recovery(state_dir)
        assert not active[0].exists()
        assert (state_dir / "prd.md").read_bytes() == prd_bytes
        assert (state_dir / "scan.db").read_bytes() == scan_bytes
        retried = scan_module.run_scan_and_report(state_dir, tmp_path)

        assert retried["seeded"] is None
        assert (state_dir / "prd.md").read_bytes() == prd_bytes
        backend = scan_module._open_backend(state_dir)
        try:
            assert len(backend.list_prds()) == 1
            assert len(backend.list_tasks()) == 4
        finally:
            backend.close()

    def test_recovery_refuses_oversized_fixed_marker_without_read_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_module = importlib.import_module("anvil.cli.scan")
        state_dir = tmp_path / ".anvil"
        state_dir.mkdir()
        _token, recovery_root = scan_module._create_scan_recovery(state_dir)
        marker = recovery_root / "scan.db.absent"
        with marker.open("r+b") as handle:
            handle.truncate(32 * 1024 * 1024)

        monkeypatch.setattr(
            Path,
            "read_bytes",
            lambda _self: pytest.fail("fixed recovery markers must be bounded"),
        )
        with pytest.raises(scan_module.ScanRecoveryError):
            scan_module._resume_scan_recovery(state_dir)
        assert recovery_root.exists()

    def test_recovery_directory_entry_count_is_bounded_before_sort(
        self, tmp_path: Path
    ) -> None:
        scan_module = importlib.import_module("anvil.cli.scan")
        state_dir = tmp_path / ".anvil"
        recovery_parent = state_dir / "recovery"
        recovery_parent.mkdir(parents=True)
        for index in range(scan_module._MAX_SCAN_RECOVERY_ENTRIES + 1):
            (recovery_parent / f".retired-scan-{index:016x}").mkdir()

        with pytest.raises(scan_module.ScanArtifactError):
            scan_module._resume_scan_recovery(state_dir)

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
        (tmp_path / "src" / "pkg" / "core.py").write_text(
            "def core():\n    return 99\n"
        )
        (tmp_path / "src" / "pkg" / "added.py").write_text(
            "def added():\n    return 4\n"
        )
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
            runner.invoke(
                app, ["scan", "--json", "--force"], catch_exceptions=False
            ).output
        )
        assert forced["data"]["seeded"] is not None
        assert forced["data"]["seeded"]["tasks"] >= 1
        conn = sqlite3.connect(str(tmp_path / ".anvil" / "state.db"))
        try:
            revision, status = conn.execute(
                "SELECT revision, status FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            conn.close()
        assert (revision, status) == (2, "approved")

        # Repeated force-seeding must advance from the current revision rather
        # than destructively parsing and then reviewing hard-coded revision 1.
        forced_again = runner.invoke(
            app, ["scan", "--json", "--force"], catch_exceptions=False
        )
        assert forced_again.exit_code == 0, forced_again.output
        conn = sqlite3.connect(str(tmp_path / ".anvil" / "state.db"))
        try:
            revision, status = conn.execute(
                "SELECT revision, status FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            conn.close()
        assert (revision, status) == (3, "approved")

    def test_force_scan_refuses_over_limit_existing_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from anvil.cli._helpers import MAX_PRD_SOURCE_BYTES_V1

        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        authored = b"x" * (MAX_PRD_SOURCE_BYTES_V1 + 1)
        prd_path.write_bytes(authored)
        before = sqlite3.connect(str(state_dir / "state.db"))
        try:
            revision_before = before.execute(
                "SELECT revision FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            before.close()

        result = runner.invoke(
            app,
            ["scan", "--json", "--force"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "seed_rejected"
        assert "byte limit" in payload["error"]["message"]
        assert prd_path.read_bytes() == authored
        after = sqlite3.connect(str(state_dir / "state.db"))
        try:
            revision_after = after.execute(
                "SELECT revision FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            after.close()
        assert revision_after == revision_before

    def test_force_scan_refuses_existing_source_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        outside = tmp_path / "outside-prd.md"
        authored = b"# Project: Outside\n"
        outside.write_bytes(authored)
        prd_path.unlink()
        try:
            prd_path.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc.__class__.__name__}")

        result = runner.invoke(
            app,
            ["scan", "--json", "--force"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"] == {
            "code": "scan_artifact_error",
            "message": "scan artifact update failed; prior artifacts were restored",
        }
        assert prd_path.is_symlink()
        assert outside.read_bytes() == authored

    def test_force_scan_allocates_fresh_ids_after_generated_id_retirement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            conn.execute(
                "UPDATE requirements SET revision_superseded = 2 WHERE id = 'R001'"
            )
            conn.execute(
                "UPDATE prds SET revision = 2, source_revision = 2, "
                "status = 'draft', lifecycle_revision = NULL, "
                "lifecycle_source_sha256 = NULL, lifecycle_material_sha256 = NULL, "
                "lifecycle_content_event_id = NULL, review_event_id = NULL "
                "WHERE is_default = 1"
            )
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(
            app, ["scan", "--json", "--force"], catch_exceptions=False
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        generated = (state_dir / "prd.md").read_text(encoding="utf-8")
        assert "- R001:" not in generated

    def test_force_scan_ignores_unbounded_numeric_ids_when_allocating(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        huge_id = "R" + "9" * 10_000
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            conn.execute(
                "UPDATE requirements SET revision_superseded = 2 WHERE id = 'R001'"
            )
            conn.execute(
                "INSERT INTO requirements "
                "(id, prd_id, prd_section, text, source_paragraph, derived, "
                "revision_introduced, revision_superseded) "
                "VALUES (?, 'default', 'requirements', 'hostile id', NULL, 0, 1, 2)",
                (huge_id,),
            )
            conn.execute(
                "UPDATE prds SET revision = 2, source_revision = 2, "
                "status = 'draft', lifecycle_revision = NULL, "
                "lifecycle_source_sha256 = NULL, lifecycle_material_sha256 = NULL, "
                "lifecycle_content_event_id = NULL, review_event_id = NULL "
                "WHERE is_default = 1"
            )
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(
            app, ["scan", "--json", "--force"], catch_exceptions=False
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        generated = (state_dir / "prd.md").read_text(encoding="utf-8")
        generated_ids = [
            line.split(":", 1)[0][2:]
            for line in generated.splitlines()
            if line.startswith("- R") and ":" in line
        ]
        assert generated_ids
        assert "R001" not in generated_ids
        assert all(len(requirement_id) < 20 for requirement_id in generated_ids)

    def test_generated_requirement_block_canonicalizes_numeric_suffixes(self) -> None:
        from anvil.cli.scan import _next_generated_requirement_start

        zero_padded_two = "R" + "0" * 10_000 + "2"
        genuinely_huge = "R" + "9" * 10_000

        assert (
            _next_generated_requirement_start(
                {zero_padded_two, genuinely_huge},
                block_size=2,
            )
            == 3
        )
        assert _next_generated_requirement_start({genuinely_huge}, block_size=2) == 1

    def test_force_scan_treats_zero_padded_numeric_alias_as_occupied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        zero_padded_one = "R" + "0" * 10_000 + "1"
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            conn.execute(
                "INSERT INTO requirements "
                "(id, prd_id, prd_section, text, source_paragraph, derived, "
                "revision_introduced, revision_superseded) "
                "VALUES (?, 'default', 'requirements', "
                "'zero-padded alias', NULL, 0, 1, 1)",
                (zero_padded_one,),
            )
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(
            app, ["scan", "--json", "--force"], catch_exceptions=False
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["ok"] is True
        generated = (state_dir / "prd.md").read_text(encoding="utf-8")
        assert "- R001:" not in generated

    @pytest.mark.parametrize("boundary_retired", (False, True))
    def test_force_scan_avoids_live_and_retired_numeric_boundary_collisions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        boundary_retired: bool,
    ) -> None:
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        boundary_id = "R1000000000000"
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            conn.execute(
                "UPDATE requirements SET revision_superseded = 2 WHERE id = 'R001'"
            )
            conn.execute(
                "INSERT INTO requirements "
                "(id, prd_id, prd_section, text, source_paragraph, derived, "
                "revision_introduced, revision_superseded) "
                "VALUES ('R999999999999', 'default', 'requirements', "
                "'lower boundary', NULL, 0, 1, 2)"
            )
            conn.execute(
                "INSERT INTO requirements "
                "(id, prd_id, prd_section, text, source_paragraph, derived, "
                "revision_introduced, revision_superseded) "
                "VALUES (?, 'default', 'requirements', "
                "'authored boundary', NULL, 0, 1, ?)",
                (boundary_id, 2 if boundary_retired else None),
            )
            conn.execute(
                "UPDATE prds SET revision = 2, source_revision = 2, "
                "status = 'draft', lifecycle_revision = NULL, "
                "lifecycle_source_sha256 = NULL, lifecycle_material_sha256 = NULL, "
                "lifecycle_content_event_id = NULL, review_event_id = NULL "
                "WHERE is_default = 1"
            )
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(
            app, ["scan", "--json", "--force"], catch_exceptions=False
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["ok"] is True
        generated = (state_dir / "prd.md").read_text(encoding="utf-8")
        assert f"- {boundary_id}:" not in generated
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            text, revision_superseded = conn.execute(
                "SELECT text, revision_superseded FROM requirements WHERE id = ?",
                (boundary_id,),
            ).fetchone()
        finally:
            conn.close()
        assert text == "authored boundary"
        assert revision_superseded == (2 if boundary_retired else 3)

    def test_failed_force_scan_preserves_authored_prd_and_json_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anvil.cli._sample as sample_module

        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        state_dir = tmp_path / ".anvil"
        authored = "# Project: Authored\n\nDo not overwrite.\n"
        (state_dir / "prd.md").write_text(authored, encoding="utf-8")

        def refuse_seed(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise sample_module.SampleSeedError("synthetic seed refusal")

        monkeypatch.setattr(sample_module, "seed_pipeline_from_prd", refuse_seed)
        result = runner.invoke(
            app, ["scan", "--json", "--force"], catch_exceptions=False
        )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "seed_rejected"
        assert (state_dir / "prd.md").read_text(encoding="utf-8") == authored
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            assert conn.execute("SELECT COUNT(*) FROM prds").fetchone() == (0,)
        finally:
            conn.close()
        assert list(state_dir.glob(".prd.md.*.tmp")) == []

    def test_interrupted_force_scan_restores_authored_prd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        import anvil.cli._sample as sample_module

        scan_module = importlib.import_module("anvil.cli.scan")
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        authored = b"# Project: Authored\r\n\r\nKeep these exact bytes.\r\n"
        prd_path.write_bytes(authored)
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            revision_before = conn.execute(
                "SELECT revision FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            conn.close()

        def interrupt_seed(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise KeyboardInterrupt

        monkeypatch.setattr(sample_module, "seed_pipeline_from_prd", interrupt_seed)
        with pytest.raises(KeyboardInterrupt):
            scan_module.run_scan_and_report(state_dir, tmp_path, force=True)

        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            revision_after = conn.execute(
                "SELECT revision FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            conn.close()
        assert revision_after == revision_before
        assert prd_path.read_bytes() == authored
        assert list(state_dir.glob(".prd.md.*.tmp")) == []

    @pytest.mark.parametrize("failure_point", ("fsync", "after_replace"))
    def test_publish_base_exception_restores_and_cleans_staging(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure_point: str,
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        authored = b"# Project: Authored\r\n\r\nKeep exact source bytes.\r\n"
        prd_path.write_bytes(authored)
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            revision_before = conn.execute(
                "SELECT revision FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            conn.close()

        if failure_point == "fsync":
            monkeypatch.setattr(
                scan_module,
                "_fsync_prd_staging",
                lambda _fd: (_ for _ in ()).throw(KeyboardInterrupt()),
            )
        else:
            real_capture_replace = scan_module._atomic_capture_replace
            interrupted = False

            def capture_then_interrupt(
                path: Path,
                replacement: Path,
                captured: Path,
            ) -> None:
                nonlocal interrupted
                real_capture_replace(path, replacement, captured)
                if path == prd_path and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt

            monkeypatch.setattr(
                scan_module,
                "_atomic_capture_replace",
                capture_then_interrupt,
            )

        with pytest.raises(KeyboardInterrupt):
            scan_module.run_scan_and_report(state_dir, tmp_path, force=True)

        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            revision_after = conn.execute(
                "SELECT revision FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            conn.close()
        assert revision_after == revision_before
        assert prd_path.read_bytes() == authored
        assert list(state_dir.glob(".prd.md.*.tmp")) == []

    def test_failed_seed_rollback_holds_state_lock_through_source_restore(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        import anvil.cli._sample as sample_module

        scan_module = importlib.import_module("anvil.cli.scan")
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        authored = b"# Project: Authored\n"
        prd_path.write_bytes(authored)
        contender = scan_module._open_backend(state_dir)
        attempted = threading.Event()
        acquired = threading.Event()
        contender_thread: threading.Thread | None = None

        def contend_for_state_lock() -> None:
            attempted.set()
            with contender._append_lock():
                acquired.set()

        real_restore = scan_module._restore_prd_source_if_unchanged

        def verify_locked_restore(*args: object, **kwargs: object) -> None:
            nonlocal contender_thread
            contender_thread = threading.Thread(target=contend_for_state_lock)
            contender_thread.start()
            assert attempted.wait(timeout=1)
            assert not acquired.wait(timeout=0.1)
            real_restore(*args, **kwargs)

        def interrupt_seed(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise KeyboardInterrupt

        monkeypatch.setattr(sample_module, "seed_pipeline_from_prd", interrupt_seed)
        monkeypatch.setattr(
            scan_module,
            "_restore_prd_source_if_unchanged",
            verify_locked_restore,
        )
        try:
            with pytest.raises(KeyboardInterrupt):
                scan_module.run_scan_and_report(state_dir, tmp_path, force=True)
            assert contender_thread is not None
            contender_thread.join(timeout=2)
            assert not contender_thread.is_alive()
            assert acquired.is_set()
        finally:
            contender.close()

        assert prd_path.read_bytes() == authored

    def test_atomic_replace_never_renames_canonical_source_away(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        authored = b"# Project: Authored\n"
        generated = b"# Project: Generated\n"
        prd_path.write_bytes(authored)
        real_replace = scan_module.os.replace
        missing_after_replace: list[bool] = []

        def observe_replace(
            source: str | os.PathLike[str],
            destination: str | os.PathLike[str],
        ) -> None:
            real_replace(source, destination)
            if Path(source) == prd_path:
                missing_after_replace.append(not prd_path.exists())
            if Path(destination) == prd_path:
                assert prd_path.exists()

        monkeypatch.setattr(scan_module.os, "replace", observe_replace)

        assert scan_module._atomic_replace_prd(
            prd_path,
            generated,
            operation="publish generated",
            expected=authored,
        )
        assert missing_after_replace == []
        assert prd_path.read_bytes() == generated
        assert list(tmp_path.glob(".prd.md.*.tmp")) == []

    def test_hard_kill_after_atomic_replace_keeps_canonical_source(self, tmp_path: Path) -> None:
        import subprocess
        import sys
        import textwrap

        prd_path = tmp_path / "prd.md"
        authored = b"# Project: Authored\n"
        generated = b"# Project: Generated\n"
        prd_path.write_bytes(authored)
        probe = textwrap.dedent(
            """
            import importlib
            import os
            import sys
            from pathlib import Path

            scan_module = importlib.import_module("anvil.cli.scan")
            prd_path = Path(sys.argv[1])
            real_capture_replace = scan_module._atomic_capture_replace

            def kill_after_capture_replace(path, replacement, captured):
                real_capture_replace(path, replacement, captured)
                os._exit(73)

            scan_module._atomic_capture_replace = kill_after_capture_replace
            scan_module._atomic_replace_prd(
                prd_path,
                b"# Project: Generated\\n",
                operation="publish generated",
                expected=b"# Project: Authored\\n",
            )
            """
        )

        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", probe, str(prd_path)],
            check=False,
        )

        assert completed.returncode == 73
        assert prd_path.read_bytes() == generated
        artifacts = list(tmp_path.glob(".prd.md.*.tmp"))
        assert artifacts
        assert any(artifact.read_bytes() == authored for artifact in artifacts)

    def test_forced_scan_preserves_authored_source_when_all_links_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        authored = b"# Project: Authored\r\n\r\nKeep exact bytes.\r\n"
        prd_path.write_bytes(authored)
        before = scan_module._open_backend(state_dir)
        try:
            revision_before = before.get_prd().revision
        finally:
            before.close()

        def refuse_all_links(*_args: object, **_kwargs: object) -> None:
            raise PermissionError(1, "synthetic hard-link refusal")

        monkeypatch.setattr(scan_module.os, "link", refuse_all_links)
        result = runner.invoke(
            app,
            ["scan", "--json", "--force"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "seed_rejected"
        assert prd_path.read_bytes() == authored
        after = scan_module._open_backend(state_dir)
        try:
            assert after.get_prd().revision == revision_before
        finally:
            after.close()
        assert list(state_dir.glob(".prd.md.*.tmp")) == []

    def test_two_non_force_first_scans_seed_only_once(self, tmp_path: Path) -> None:
        import importlib

        from anvil.scan.model import scan_working_tree

        scan_module = importlib.import_module("anvil.cli.scan")
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        model = scan_working_tree(tmp_path)
        barrier = threading.Barrier(2)
        outcome_lock = threading.Lock()
        decisions: list[str | None] = []
        outcomes: list[dict[str, object] | None] = []

        def first_scan() -> None:
            decision = scan_module._should_seed(
                state_dir,
                prd_path,
                force=False,
            )
            with outcome_lock:
                decisions.append(decision)
            barrier.wait(timeout=5)
            seeded = scan_module._seed_draft(
                state_dir,
                tmp_path,
                model,
                revalidate_first_seed=decision == "no_prd",
            )
            with outcome_lock:
                outcomes.append(seeded)

        threads = [threading.Thread(target=first_scan) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not any(thread.is_alive() for thread in threads)
        assert decisions == ["no_prd", "no_prd"]
        assert sum(outcome is not None for outcome in outcomes) == 1
        backend = scan_module._open_backend(state_dir)
        try:
            assert backend.get_prd().revision == 1
        finally:
            backend.close()
        assert list(state_dir.glob(".prd.md.*.tmp")) == []

    def test_failed_seed_preserves_concurrent_source_writer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        import anvil.cli._sample as sample_module

        scan_module = importlib.import_module("anvil.cli.scan")
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        prd_path.write_bytes(b"# Project: Prior source\n")
        concurrent = b"# Project: Concurrent writer\n"

        def edit_then_interrupt(*_args: object, **_kwargs: object) -> dict[str, object]:
            prd_path.write_bytes(concurrent)
            raise KeyboardInterrupt

        monkeypatch.setattr(
            sample_module,
            "seed_pipeline_from_prd",
            edit_then_interrupt,
        )
        with pytest.raises(KeyboardInterrupt):
            scan_module.run_scan_and_report(state_dir, tmp_path, force=True)

        assert prd_path.read_bytes() == concurrent
        assert list(state_dir.glob(".prd.md.*.tmp")) == []

    def test_publish_preserves_writer_arriving_after_ownership_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        prior = b"# Project: Prior source\n"
        concurrent = b"# Project: Writer after ownership claim\n"
        prd_path.write_bytes(prior)
        real_read_claimed = scan_module._read_claimed_prd_source
        writer_ran = False

        def writer_wins_after_claim(path: Path, claim: Path) -> Any:
            nonlocal writer_ran
            source = real_read_claimed(path, claim)
            if path.name.startswith(".prd.md.owned.") and not writer_ran:
                writer_ran = True
                prd_path.write_bytes(concurrent)
            return source

        monkeypatch.setattr(
            scan_module,
            "_read_claimed_prd_source",
            writer_wins_after_claim,
        )

        published = scan_module._atomic_replace_prd(
            prd_path,
            b"# Project: Generated\n",
            operation="publish generated",
            expected=prior,
        )

        assert published is None
        assert writer_ran is True
        assert prd_path.read_bytes() == concurrent
        assert list(tmp_path.glob(".prd.md.*.tmp")) == []

    def test_publish_preserves_writer_arriving_at_final_exchange(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        writer_stage = tmp_path / "writer.tmp"
        prior = b"# Project: Prior source\n"
        concurrent = b"# Project: Writer at final exchange\n"
        prd_path.write_bytes(prior)
        writer_stage.write_bytes(concurrent)
        real_capture_replace = scan_module._atomic_capture_replace
        writer_ran = False

        def writer_wins_at_exchange(
            path: Path, replacement: Path, captured: Path
        ) -> None:
            nonlocal writer_ran
            if path == prd_path and not writer_ran:
                writer_ran = True
                os.replace(writer_stage, prd_path)
            real_capture_replace(path, replacement, captured)

        monkeypatch.setattr(
            scan_module,
            "_atomic_capture_replace",
            writer_wins_at_exchange,
        )

        published = scan_module._atomic_replace_prd(
            prd_path,
            b"# Project: Generated\n",
            operation="publish generated",
            expected=prior,
        )

        assert published is None
        assert writer_ran is True
        assert prd_path.read_bytes() == concurrent
        assert list(tmp_path.glob(".prd.md.*.tmp")) == []

    def test_publish_preserves_writer_replacing_path_after_exchange(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        writer_stage = tmp_path / "writer.tmp"
        prior = b"# Project: Prior source\n"
        generated = b"# Project: Generated\n"
        concurrent = b"# Project: Writer after exchange\n"
        prd_path.write_bytes(prior)
        writer_stage.write_bytes(concurrent)
        real_capture_replace = scan_module._atomic_capture_replace
        writer_ran = False

        def writer_wins_after_exchange(
            path: Path, replacement: Path, captured: Path
        ) -> None:
            nonlocal writer_ran
            real_capture_replace(path, replacement, captured)
            if path == prd_path and not writer_ran:
                writer_ran = True
                os.replace(writer_stage, prd_path)

        monkeypatch.setattr(
            scan_module,
            "_atomic_capture_replace",
            writer_wins_after_exchange,
        )

        published = scan_module._atomic_replace_prd(
            prd_path,
            generated,
            operation="publish generated",
            expected=prior,
        )

        assert published is None
        assert writer_ran is True
        assert prd_path.read_bytes() == concurrent
        recovery = list(tmp_path.glob(".prd.md.recovery.*.bak"))
        assert len(recovery) == 1
        assert recovery[0].read_bytes() == prior
        assert list(tmp_path.glob(".prd.md.*.tmp")) == []

    def test_publish_refuses_generated_inode_write_before_snapshot_handoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        prior = b"# Project: Prior source\n"
        generated = b"# Project: Generated\n"
        concurrent = b"# Project: Writer before handoff\n"
        prd_path.write_bytes(prior)
        real_retain = scan_module._retain_displaced_source
        writer_ran = False

        def write_generated_before_handoff(path: Path, captured: Path) -> Path:
            nonlocal writer_ran
            if path == prd_path and not writer_ran:
                writer_ran = True
                path.write_bytes(concurrent)
            return real_retain(path, captured)

        monkeypatch.setattr(
            scan_module,
            "_retain_displaced_source",
            write_generated_before_handoff,
        )

        published = scan_module._atomic_replace_prd(
            prd_path,
            generated,
            operation="publish generated",
            expected=prior,
        )

        assert published is None
        assert writer_ran is True
        assert prd_path.read_bytes() == concurrent
        recovery = list(tmp_path.glob(".prd.md.recovery.*.bak"))
        assert len(recovery) == 1
        assert recovery[0].read_bytes() == prior
        assert list(tmp_path.glob(".prd.md.*.tmp")) == []

    def test_missing_source_publish_verifies_final_inode_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        generated = b"# Project: Generated\n"
        concurrent = b"# Project: Writer before handoff\n"
        real_link = scan_module.os.link
        writer_ran = False

        def link_then_write(
            source: str | os.PathLike[str],
            destination: str | os.PathLike[str],
        ) -> None:
            nonlocal writer_ran
            real_link(source, destination)
            if Path(destination) == prd_path and not writer_ran:
                writer_ran = True
                prd_path.write_bytes(concurrent)

        monkeypatch.setattr(scan_module.os, "link", link_then_write)

        published = scan_module._atomic_replace_prd(
            prd_path,
            generated,
            operation="publish generated",
            expected=scan_module._SOURCE_MISSING,
        )

        assert published is None
        assert writer_ran is True
        assert prd_path.read_bytes() == concurrent
        assert list(tmp_path.glob(".prd.md.*.tmp")) == []

    def test_publish_retains_late_write_through_preopened_handle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        prior = b"# Project: Prior source\n"
        generated = b"# Project: Generated\n"
        concurrent = b"# Project: Writer after final validation\n"
        prd_path.write_bytes(prior)

        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            create_file.restype = wintypes.HANDLE
            writer = create_file(
                str(prd_path),
                0x40000000,  # GENERIC_WRITE
                0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
                None,
                3,  # OPEN_EXISTING
                0x00000080,  # FILE_ATTRIBUTE_NORMAL
                None,
            )
            assert writer not in (None, wintypes.HANDLE(-1).value)

            def late_write() -> None:
                set_pointer = kernel32.SetFilePointer
                set_pointer.argtypes = [
                    wintypes.HANDLE,
                    wintypes.LONG,
                    ctypes.POINTER(wintypes.LONG),
                    wintypes.DWORD,
                ]
                set_pointer.restype = wintypes.DWORD
                assert set_pointer(writer, 0, None, 0) == 0
                written = wintypes.DWORD()
                write_file = kernel32.WriteFile
                write_file.argtypes = [
                    wintypes.HANDLE,
                    wintypes.LPCVOID,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                    wintypes.LPVOID,
                ]
                write_file.restype = wintypes.BOOL
                assert write_file(
                    writer,
                    concurrent,
                    len(concurrent),
                    ctypes.byref(written),
                    None,
                )
                assert written.value == len(concurrent)
                assert kernel32.FlushFileBuffers(writer)

            def close_writer() -> None:
                assert kernel32.CloseHandle(writer)

        else:
            writer = os.open(prd_path, os.O_WRONLY)

            def late_write() -> None:
                os.lseek(writer, 0, os.SEEK_SET)
                assert os.write(writer, concurrent) == len(concurrent)
                os.ftruncate(writer, len(concurrent))
                os.fsync(writer)

            def close_writer() -> None:
                os.close(writer)

        real_retain = scan_module._retain_displaced_source
        writer_ran = False

        def write_after_validation(path: Path, captured: Path) -> Path:
            nonlocal writer_ran
            writer_ran = True
            late_write()
            return real_retain(path, captured)

        monkeypatch.setattr(
            scan_module,
            "_retain_displaced_source",
            write_after_validation,
        )
        try:
            published = scan_module._atomic_replace_prd(
                prd_path,
                generated,
                operation="publish generated",
                expected=prior,
            )
        finally:
            close_writer()

        assert published is not None
        assert writer_ran is True
        assert prd_path.read_bytes() == generated
        recovery = list(tmp_path.glob(".prd.md.recovery.*.bak"))
        assert len(recovery) == 1
        assert recovery[0].read_bytes() == concurrent
        assert list(tmp_path.glob(".prd.md.*.tmp")) == []

    def test_remove_preserves_writer_arriving_after_ownership_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        generated = b"# Project: Generated\n"
        concurrent = b"# Project: Writer after ownership claim\n"
        prd_path.write_bytes(generated)
        real_read_claimed = scan_module._read_claimed_prd_source
        writer_ran = False

        def writer_wins_after_check(path: Path, claim: Path) -> Any:
            nonlocal writer_ran
            source = real_read_claimed(path, claim)
            if path.name.startswith(".prd.md.owned.") and not writer_ran:
                writer_ran = True
                prd_path.write_bytes(concurrent)
            return source

        monkeypatch.setattr(
            scan_module,
            "_read_claimed_prd_source",
            writer_wins_after_check,
        )

        removed = scan_module._atomic_remove_prd(
            prd_path,
            expected=generated,
            operation="remove generated after seed failure",
        )

        assert removed is True
        assert writer_ran is True
        assert prd_path.read_bytes() == concurrent
        assert list(tmp_path.glob(".prd.md.*.tmp")) == []

    def test_remove_mismatch_restores_without_hardlinks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        generated = b"# Project: Generated\n"
        concurrent = b"# Project: Changed before removal\n"
        prd_path.write_bytes(concurrent)

        def refuse_hardlinks(*_args: object, **_kwargs: object) -> None:
            raise OSError(1, "synthetic hard-link refusal")

        monkeypatch.setattr(scan_module.os, "link", refuse_hardlinks)

        removed = scan_module._atomic_remove_prd(
            prd_path,
            expected=generated,
            operation="remove generated after seed failure",
        )

        assert removed is False
        assert prd_path.read_bytes() == concurrent
        assert list(tmp_path.glob(".prd.md.*.tmp")) == []

    def test_remove_mismatch_preserves_new_writer_and_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        writer_stage = tmp_path / "writer.tmp"
        generated = b"# Project: Generated\n"
        displaced = b"# Project: Changed before removal\n"
        concurrent = b"# Project: Writer during restoration\n"
        prd_path.write_bytes(displaced)
        writer_stage.write_bytes(concurrent)
        real_move = scan_module._atomic_move_no_replace
        writer_ran = False

        def writer_wins_before_restore(source: Path, destination: Path) -> None:
            nonlocal writer_ran
            if destination == prd_path and not writer_ran:
                writer_ran = True
                os.replace(writer_stage, prd_path)
            real_move(source, destination)

        monkeypatch.setattr(
            scan_module,
            "_atomic_move_no_replace",
            writer_wins_before_restore,
        )

        removed = scan_module._atomic_remove_prd(
            prd_path,
            expected=generated,
            operation="remove generated after seed failure",
        )

        assert removed is False
        assert writer_ran is True
        assert prd_path.read_bytes() == concurrent
        recovery = list(tmp_path.glob(".prd.md.recovery.*.bak"))
        assert len(recovery) == 1
        assert recovery[0].read_bytes() == displaced
        assert list(tmp_path.glob(".prd.md.*.tmp")) == []

    def test_remove_retains_late_write_through_preopened_handle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ctypes
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        generated = b"# Project: Generated\n"
        concurrent = b"# Project: Writer after removal validation\n"
        prd_path.write_bytes(generated)

        if os.name == "nt":
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            create_file.restype = wintypes.HANDLE
            writer = create_file(
                str(prd_path),
                0x40000000,  # GENERIC_WRITE
                0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
                None,
                3,  # OPEN_EXISTING
                0x00000080,  # FILE_ATTRIBUTE_NORMAL
                None,
            )
            assert writer not in (None, wintypes.HANDLE(-1).value)

            def late_write() -> None:
                set_pointer = kernel32.SetFilePointer
                set_pointer.argtypes = [
                    wintypes.HANDLE,
                    wintypes.LONG,
                    ctypes.POINTER(wintypes.LONG),
                    wintypes.DWORD,
                ]
                set_pointer.restype = wintypes.DWORD
                assert set_pointer(writer, 0, None, 0) == 0
                written = wintypes.DWORD()
                write_file = kernel32.WriteFile
                write_file.argtypes = [
                    wintypes.HANDLE,
                    wintypes.LPCVOID,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                    wintypes.LPVOID,
                ]
                write_file.restype = wintypes.BOOL
                assert write_file(
                    writer,
                    concurrent,
                    len(concurrent),
                    ctypes.byref(written),
                    None,
                )
                assert written.value == len(concurrent)
                assert kernel32.FlushFileBuffers(writer)

            def close_writer() -> None:
                assert kernel32.CloseHandle(writer)

        else:
            writer = os.open(prd_path, os.O_WRONLY)

            def late_write() -> None:
                os.lseek(writer, 0, os.SEEK_SET)
                assert os.write(writer, concurrent) == len(concurrent)
                os.ftruncate(writer, len(concurrent))
                os.fsync(writer)

            def close_writer() -> None:
                os.close(writer)

        real_retain = scan_module._retain_displaced_source
        writer_ran = False

        def write_after_validation(path: Path, captured: Path) -> Path:
            nonlocal writer_ran
            writer_ran = True
            late_write()
            return real_retain(path, captured)

        monkeypatch.setattr(
            scan_module,
            "_retain_displaced_source",
            write_after_validation,
        )
        try:
            removed = scan_module._atomic_remove_prd(
                prd_path,
                expected=generated,
                operation="remove generated after seed failure",
            )
        finally:
            close_writer()

        assert removed is True
        assert writer_ran is True
        assert not prd_path.exists()
        recovery = list(tmp_path.glob(".prd.md.recovery.*.bak"))
        assert len(recovery) == 1
        assert recovery[0].read_bytes() == concurrent
        assert list(tmp_path.glob(".prd.md.*.tmp")) == []

    def test_cancelled_publish_recovery_never_uses_path_read_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        prior = b"# Project: Prior source\n"
        generated = b"# Project: Generated\n"
        prd_path.write_bytes(prior)
        real_capture_replace = scan_module._atomic_capture_replace
        interrupted = False

        def interrupt_first_exchange(
            path: Path, replacement: Path, captured: Path
        ) -> None:
            nonlocal interrupted
            real_capture_replace(path, replacement, captured)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt

        def forbid_unbounded_read(_path: Path) -> bytes:
            raise AssertionError("rollback must not call Path.read_bytes")

        monkeypatch.setattr(
            scan_module,
            "_atomic_capture_replace",
            interrupt_first_exchange,
        )
        monkeypatch.setattr(Path, "read_bytes", forbid_unbounded_read)

        with pytest.raises(KeyboardInterrupt):
            scan_module._atomic_replace_prd(
                prd_path,
                generated,
                operation="publish generated",
                expected=prior,
            )

        with prd_path.open("rb") as restored:
            assert restored.read() == prior
        assert list(tmp_path.glob(".prd.md.*.tmp")) == []

    @pytest.mark.parametrize("writer_kind", ("oversize", "non_regular"))
    def test_cancelled_publish_recovery_bounds_hostile_writer_source(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        writer_kind: str,
    ) -> None:
        import importlib

        from anvil.cli._helpers import MAX_PRD_SOURCE_BYTES_V1

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        prior = b"# Project: Prior source\n"
        generated = b"# Project: Generated\n"
        prd_path.write_bytes(prior)
        real_capture_replace = scan_module._atomic_capture_replace
        interrupted = False

        def install_hostile_source_then_interrupt(
            path: Path, replacement: Path, captured: Path
        ) -> None:
            nonlocal interrupted
            real_capture_replace(path, replacement, captured)
            if interrupted:
                return
            interrupted = True
            if writer_kind == "oversize":
                path.write_bytes(b"x" * (MAX_PRD_SOURCE_BYTES_V1 + 1))
            else:
                path.unlink()
                path.mkdir()
            raise KeyboardInterrupt

        monkeypatch.setattr(
            scan_module,
            "_atomic_capture_replace",
            install_hostile_source_then_interrupt,
        )

        with pytest.raises(KeyboardInterrupt):
            scan_module._atomic_replace_prd(
                prd_path,
                generated,
                operation="publish generated",
                expected=prior,
            )

        if writer_kind == "oversize":
            assert prd_path.stat().st_size == MAX_PRD_SOURCE_BYTES_V1 + 1
        else:
            assert prd_path.is_dir()
        artifacts = list(tmp_path.glob(".prd.md.*.tmp"))
        assert artifacts
        assert any(artifact.stat().st_size == len(prior) for artifact in artifacts)

    def test_restore_inspection_never_uses_path_read_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        concurrent = b"# Project: Concurrent writer\n"
        prd_path.write_bytes(concurrent)

        def forbid_unbounded_read(_path: Path) -> bytes:
            raise AssertionError("restore must not call Path.read_bytes")

        monkeypatch.setattr(Path, "read_bytes", forbid_unbounded_read)

        scan_module._restore_prd_source_if_unchanged(
            prd_path,
            generated=b"# Project: Generated\n",
            prior=b"# Project: Prior\n",
        )

        with prd_path.open("rb") as retained:
            assert retained.read() == concurrent

    def test_restore_inspection_refuses_oversize_source(self, tmp_path: Path) -> None:
        import importlib

        from anvil.cli._helpers import MAX_PRD_SOURCE_BYTES_V1
        from anvil.cli._sample import SampleSeedError

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        prd_path.write_bytes(b"x" * (MAX_PRD_SOURCE_BYTES_V1 + 1))

        with pytest.raises(SampleSeedError, match="byte limit"):
            scan_module._restore_prd_source_if_unchanged(
                prd_path,
                generated=b"# Project: Generated\n",
                prior=b"# Project: Prior\n",
            )

    def test_restore_inspection_refuses_non_regular_source(self, tmp_path: Path) -> None:
        import importlib

        from anvil.cli._sample import SampleSeedError

        scan_module = importlib.import_module("anvil.cli.scan")
        prd_path = tmp_path / "prd.md"
        prd_path.mkdir()

        with pytest.raises(SampleSeedError, match="regular contained file"):
            scan_module._restore_prd_source_if_unchanged(
                prd_path,
                generated=b"# Project: Generated\n",
                prior=b"# Project: Prior\n",
            )

    def test_rollback_rechecks_source_at_atomic_replace_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        import anvil.cli._sample as sample_module

        scan_module = importlib.import_module("anvil.cli.scan")
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        prd_path.write_bytes(b"# Project: Prior source\n")
        concurrent = b"# Project: Writer in restore boundary\n"
        real_atomic_replace = scan_module._atomic_replace_prd

        def edit_at_restore_boundary(
            path: Path,
            content: bytes,
            *,
            operation: str,
            expected: bytes | object | None = None,
        ) -> Any:
            if operation == "restore prior":
                path.write_bytes(concurrent)
            return real_atomic_replace(
                path,
                content,
                operation=operation,
                expected=expected,
            )

        def interrupt_seed(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise KeyboardInterrupt

        monkeypatch.setattr(sample_module, "seed_pipeline_from_prd", interrupt_seed)
        monkeypatch.setattr(
            scan_module,
            "_atomic_replace_prd",
            edit_at_restore_boundary,
        )
        with pytest.raises(KeyboardInterrupt):
            scan_module.run_scan_and_report(state_dir, tmp_path, force=True)

        assert prd_path.read_bytes() == concurrent
        assert list(state_dir.glob(".prd.md.*.tmp")) == []

    @pytest.mark.parametrize("failure_point", ("write", "replace"))
    def test_atomic_publish_failure_keeps_revision_and_returns_json_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure_point: str,
    ) -> None:
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")

        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        authored = b"# Project: Authored\r\n\r\nKeep these exact bytes.\r\n"
        prd_path.write_bytes(authored)
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            revision_before = conn.execute(
                "SELECT revision FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            conn.close()

        if failure_point == "write":

            def refuse_sync(_fd: int) -> None:
                raise OSError(5, "synthetic write refusal")

            monkeypatch.setattr(scan_module, "_fsync_prd_staging", refuse_sync)
        else:
            def refuse_publish(
                path: Path,
                _replacement: Path,
                _captured: Path,
            ) -> None:
                if path == prd_path:
                    raise OSError(13, "synthetic replace refusal")

            monkeypatch.setattr(
                scan_module,
                "_atomic_capture_replace",
                refuse_publish,
            )
        result = runner.invoke(
            app, ["scan", "--json", "--force"], catch_exceptions=False
        )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "seed_rejected"
        assert "publish generated" in payload["error"]["message"]
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            revision_after = conn.execute(
                "SELECT revision FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            conn.close()
        assert revision_after == revision_before
        assert prd_path.read_bytes() == authored
        assert list(state_dir.glob(".prd.md.*.tmp")) == []

    def test_late_seed_failure_keeps_generated_source_after_state_advances(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anvil.cli._sample as sample_module

        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        authored = b"# Project: Concurrent authored source\n"
        prd_path.write_bytes(authored)
        real_seed = sample_module.seed_pipeline_from_prd

        def seed_then_fail(*args: object, **kwargs: object) -> dict[str, object]:
            real_seed(*args, **kwargs)  # type: ignore[arg-type]
            raise sample_module.SampleSeedError("synthetic late seed refusal")

        monkeypatch.setattr(sample_module, "seed_pipeline_from_prd", seed_then_fail)
        result = runner.invoke(
            app, ["scan", "--json", "--force"], catch_exceptions=False
        )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "seed_rejected"
        assert prd_path.read_bytes() != authored
        assert prd_path.read_text(encoding="utf-8").startswith(
            "# Project: Scan Fixture"
        )
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            revision = conn.execute(
                "SELECT revision FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            conn.close()
        assert revision == (2,)

    def test_late_seed_interruption_keeps_generated_source_after_state_advances(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        import anvil.cli._sample as sample_module

        scan_module = importlib.import_module("anvil.cli.scan")
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        authored = b"# Project: Concurrent authored source\n"
        prd_path.write_bytes(authored)
        real_seed = sample_module.seed_pipeline_from_prd

        def seed_then_interrupt(*args: object, **kwargs: object) -> dict[str, object]:
            real_seed(*args, **kwargs)  # type: ignore[arg-type]
            raise KeyboardInterrupt

        monkeypatch.setattr(
            sample_module,
            "seed_pipeline_from_prd",
            seed_then_interrupt,
        )
        with pytest.raises(KeyboardInterrupt):
            scan_module.run_scan_and_report(state_dir, tmp_path, force=True)

        assert prd_path.read_bytes() != authored
        assert prd_path.read_text(encoding="utf-8").startswith(
            "# Project: Scan Fixture"
        )
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            revision = conn.execute(
                "SELECT revision FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            conn.close()
        assert revision == (2,)

    def test_log_ahead_interruption_keeps_generated_source_for_forward_catch_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins
        import importlib

        scan_module = importlib.import_module("anvil.cli.scan")
        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["scan"], catch_exceptions=False).exit_code == 0

        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        events_path = state_dir / "events.jsonl"
        authored = b"# Project: Authored\n\nKeep these exact bytes.\n"
        prd_path.write_bytes(authored)
        real_open = builtins.open
        interrupted = False

        class InterruptAfterLogClose:
            def __init__(self, handle: object) -> None:
                self.handle = handle

            def __enter__(self) -> InterruptAfterLogClose:
                return self

            def __exit__(self, *_args: object) -> None:
                self.handle.close()  # type: ignore[attr-defined]
                raise KeyboardInterrupt

            def write(self, content: str) -> int:
                return self.handle.write(content)  # type: ignore[attr-defined,no-any-return]

            def flush(self) -> None:
                self.handle.flush()  # type: ignore[attr-defined]

            def fileno(self) -> int:
                return self.handle.fileno()  # type: ignore[attr-defined,no-any-return]

        def interrupt_after_log_append(
            file: Any,
            mode: str = "r",
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            nonlocal interrupted
            handle = real_open(file, mode, *args, **kwargs)
            if (
                not interrupted
                and mode == "a"
                and isinstance(file, (str, Path))
                and Path(file) == events_path
            ):
                interrupted = True
                return InterruptAfterLogClose(handle)
            return handle

        with monkeypatch.context() as patcher:
            patcher.setattr(builtins, "open", interrupt_after_log_append)
            with pytest.raises(KeyboardInterrupt):
                scan_module.run_scan_and_report(state_dir, tmp_path, force=True)

        assert interrupted is True
        assert prd_path.read_bytes() != authored
        generated = prd_path.read_bytes()
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            revision_before_reopen = conn.execute(
                "SELECT revision FROM prds WHERE is_default = 1"
            ).fetchone()
        finally:
            conn.close()
        assert revision_before_reopen == (1,)

        backend = scan_module._open_backend(state_dir)
        try:
            assert backend.get_prd().revision == 2
        finally:
            backend.close()
        assert prd_path.read_bytes() == generated
        assert list(state_dir.glob(".prd.md.*.tmp")) == []

    def test_pre_state_seed_failure_removes_new_generated_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anvil.cli._sample as sample_module

        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        state_dir = tmp_path / ".anvil"
        prd_path = state_dir / "prd.md"
        assert not prd_path.exists()

        def refuse_seed(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise sample_module.SampleSeedError("synthetic seed refusal")

        monkeypatch.setattr(sample_module, "seed_pipeline_from_prd", refuse_seed)
        result = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)

        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "seed_rejected"
        assert not prd_path.exists()
        assert list(state_dir.glob(".prd.md.*.tmp")) == []

    def test_first_scan_atomic_planning_failure_leaves_no_partial_graph(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from anvil.state.sqlite import SqliteBackend

        _make_fixture_repo(tmp_path)
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        state_dir = tmp_path / ".anvil"
        events_path = state_dir / "events.jsonl"
        before = events_path.read_bytes()
        original = SqliteBackend._write_task_created  # noqa: SLF001
        task_writes = 0

        def fail_second_task(
            backend: SqliteBackend, *args: object, **kwargs: object
        ) -> None:
            nonlocal task_writes
            task_writes += 1
            if task_writes == 2:
                raise RuntimeError("injected second task failure")
            original(backend, *args, **kwargs)  # type: ignore[arg-type]

        with monkeypatch.context() as injected:
            injected.setattr(
                SqliteBackend,
                "_write_task_created",
                fail_second_task,
            )
            failed = runner.invoke(
                app,
                ["scan", "--json"],
                catch_exceptions=False,
            )

        assert failed.exit_code == 1
        assert json.loads(failed.output)["error"]["code"] == "seed_rejected"
        assert events_path.read_bytes() == before
        assert not (state_dir / "prd.md").exists()
        assert not (state_dir / "scan.db").exists()
        connection = sqlite3.connect(state_dir / "state.db")
        try:
            assert connection.execute("SELECT COUNT(*) FROM prds").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM features").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        finally:
            connection.close()

        retried = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)
        assert retried.exit_code == 0, retried.output
        assert json.loads(retried.output)["data"]["seeded"] is not None


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

    @pytest.mark.parametrize(
        "failure,message",
        [
            ("loader", "Windows path API unavailable (library load failed)"),
            ("mapping", "Windows path case mapping failed"),
            ("comparison", "Windows path comparison failed"),
        ],
    )
    def test_init_from_repo_native_path_failure_is_bounded_and_seed_atomic(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: str,
        message: str,
    ) -> None:
        from anvil.planning import inference as inference_module
        from anvil.planning.inference import PathIdentityError

        _make_fixture_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            inference_module, "_uses_windows_path_identity", lambda: True
        )
        def fail_identity(path: Path) -> None:
            raise PathIdentityError(message)

        monkeypatch.setattr(
            inference_module,
            "_windows_existing_path_identity",
            fail_identity,
        )

        result = runner.invoke(
            app, ["init", "--from-repo"], catch_exceptions=False
        )

        assert result.exit_code == 1
        assert f"Error: seed planning inference refused: {message}" in result.output
        state_dir = tmp_path / ".anvil"
        events = [
            json.loads(line)
            for line in (state_dir / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert [event["action"] for event in events] == [
            "project.created",
            "state.initialized",
        ]
        assert not (state_dir / "scan.db").exists()
        assert not (state_dir / "prd.md").exists()
        connection = sqlite3.connect(state_dir / "state.db")
        try:
            assert connection.execute("SELECT COUNT(*) FROM prds").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM features").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        finally:
            connection.close()

    def test_init_from_repo_oversized_path_restores_artifacts_and_scan_retries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from anvil.planning import inference as inference_module
        from anvil.planning import template as template_module

        _make_fixture_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        oversized = "é" * 2_048 + "a"
        original_parse = template_module.parse_prd
        parse_calls = 0

        def fail_first_parse(*args: object, **kwargs: object) -> object:
            nonlocal parse_calls
            result = original_parse(*args, **kwargs)
            parse_calls += 1
            if parse_calls == 1:
                result.tasks[0].likely_files.append(oversized)
            return result

        monkeypatch.setattr(template_module, "parse_prd", fail_first_parse)
        inference_module._cached_windows_path_key.cache_clear()
        inference_module._cached_windows_paths_equal.cache_clear()
        key_before = inference_module._cached_windows_path_key.cache_info()
        comparison_before = (
            inference_module._cached_windows_paths_equal.cache_info()
        )

        failed = runner.invoke(
            app, ["init", "--from-repo"], catch_exceptions=False
        )

        message = (
            "seed planning inference refused: bundle planning requires "
            "likely-file paths no longer than 4096 UTF-8 bytes"
        )
        assert failed.exit_code == 1
        assert f"Error: {message}" in failed.output
        assert len(message) <= 4_096
        assert message.encode("cp1252")
        state_dir = tmp_path / ".anvil"
        assert not (state_dir / "scan.db").exists()
        assert not (state_dir / "prd.md").exists()
        assert inference_module._cached_windows_path_key.cache_info() == key_before
        assert (
            inference_module._cached_windows_paths_equal.cache_info()
            == comparison_before
        )
        events = [
            json.loads(line)
            for line in (state_dir / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert [event["action"] for event in events] == [
            "project.created",
            "state.initialized",
        ]

        retried = runner.invoke(app, ["scan", "--json"], catch_exceptions=False)

        assert retried.exit_code == 0, retried.output
        retry_data = json.loads(retried.output)["data"]
        assert retry_data["first_scan"] is True
        assert retry_data["seeded"] is not None
        assert retry_data["seeded"]["ready"] >= 1
