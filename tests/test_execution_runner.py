from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from anvil.execution import (
    ExecutionError,
    RunOptions,
    confirm_run,
    journal_root,
    preview_run,
    read_receipt,
)
from anvil.execution_runner import capture, integrate, process_group_absent
from tests.test_execution import execution_project as execution_project

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Owned Run adapter requires POSIX process-group custody"
)


def _wait(directory):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if (directory / "result.json").exists():
            result = read_receipt(directory / "result.json")
            launched = read_receipt(directory / "launched.json")
            try:
                os.waitpid(launched["pid"], 0)
            except ChildProcessError:
                pass
            stopped_deadline = time.monotonic() + 5
            while not process_group_absent(launched["pid"]) and time.monotonic() < stopped_deadline:
                time.sleep(0.02)
            assert process_group_absent(launched["pid"])
            return result
        time.sleep(0.02)
    pytest.fail("owned execution worker did not return")


def test_ready_run_edits_and_submits_real_native_proof(execution_project):
    backend, repo, state, _ = execution_project
    preview = preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    result = confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / result["run_id"]
    finished = _wait(directory)
    assert finished["state"] == "needs_review", finished
    assert finished["acceptance"] == "not_granted"
    assert finished["local_children_stopped"] is True
    assert Path(result["cwd"]).joinpath("README.md").read_bytes() == b"implemented\n"
    assert (repo / "README.md").read_bytes() == b"initial\n"
    assert backend.get_task("release:T001").status.value == "needs_review"
    assert backend.get_claim(result["claim_id"]).status.value == "released"
    evidence = backend.get_latest_evidence("release:T001")
    assert len(evidence.proofs) == 1
    assert evidence.proofs[0].evidence_core.claim_id == result["claim_id"]
    assert b"native-proof-ok" in (directory / "check-0.log").read_bytes()
    assert not any(
        json.loads(line)["action"] == "task.applied"
        for line in (state / "events.jsonl").read_bytes().splitlines()
    )
    from anvil.execution_runner import execute_run

    with pytest.raises(FileExistsError):
        execute_run(directory)
    assert backend.get_latest_evidence("release:T001").id == evidence.id


@pytest.mark.parametrize(
    "command,code",
    [
        (["-c", "print('x'*200000)"], "output_overflow"),
        (["-c", "raise SystemExit(3)"], "command_failed"),
        (["-c", "import time; time.sleep(60)"], "command_timeout"),
    ],
)
def test_capture_failures_preserve_output_and_stop_owned_group(tmp_path, command, code):
    tmp_path.chmod(0o700)
    with pytest.raises(ExecutionError) as error:
        capture(
            [sys.executable, *command], tmp_path, tmp_path, "check", time.monotonic() + 0.5, 131072
        )
    assert error.value.code == code
    result = read_receipt(tmp_path / "check.result.json")
    assert result["group_absent"] is True
    assert process_group_absent(result["pid"])
    assert (tmp_path / "check.log").exists()


def test_integration_checks_all_preimages_before_edit(tmp_path):
    root, output, journal = [tmp_path / name for name in ("root", "output", "journal")]
    root.mkdir()
    (output / "results" / "changed-files").mkdir(parents=True)
    journal.mkdir(mode=0o700)
    (root / "one").write_bytes(b"old")
    (root / "two").write_bytes(b"drift")
    changes = [
        {"path": name, "before_sha256": None, "after_sha256": "irrelevant"}
        for name in ("one", "two")
    ]
    with pytest.raises(ExecutionError, match="Local source changed"):
        integrate(root, output, changes, {"one": b"old", "two": b"old"}, journal)
    assert (root / "one").read_bytes() == b"old"
    assert not (journal / "integration.json").exists()


def _hold_adapter(project, tmp_path):
    import dataclasses

    import yaml

    from anvil.config import global_config_path
    from anvil.execution import digest

    registration = project[3]
    release = tmp_path / "allow-return"
    Path(registration.profile).write_text(json.dumps({"wait_file": str(release)}))
    data = dataclasses.asdict(registration)
    data["hashes"]["profile"] = digest(Path(registration.profile).read_bytes())
    global_config_path().write_text(yaml.safe_dump({"execution_adapter": data}))
    return release


def _wait_adapter(directory):
    deadline = time.monotonic() + 20
    while not (directory / "adapter" / "inputs.tar").exists():
        # The double creates its output directory before waiting for release.
        if (directory / "adapter").is_dir():
            return
        if (directory / "result.json").exists():
            pytest.fail(str(read_receipt(directory / "result.json")))
        if time.monotonic() >= deadline:
            pytest.fail("adapter did not reach its hold")
        time.sleep(0.02)


def test_two_process_confirmation_launches_once_and_unrelated_claim_progresses(
    execution_project, tmp_path
):
    import subprocess

    from anvil.execution import native_cli

    backend, repo, _, _ = execution_project
    release = _hold_adapter(execution_project, tmp_path)
    preview = preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    argv = [
        sys.executable,
        "-I",
        "-m",
        "anvil.cli",
        "run",
        "release:T001",
        "--confirm",
        preview["preview_id"],
        "--actor",
        "synthetic-owner",
        "--cwd",
        str(repo),
        "--json",
    ]
    contenders = [
        subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        for _ in range(2)
    ]
    directory = None
    try:
        results = [child.communicate(timeout=30) for child in contenders]
        successful = [
            json.loads(output)
            for child, (output, _) in zip(contenders, results, strict=True)
            if child.returncode == 0
        ]
        assert len(successful) == 1, results
        directory = journal_root(repo) / "runs" / successful[0]["data"]["run_id"]
        _wait_adapter(directory)
        # An independent native mutation completes while inference remains held.
        claimed = json.loads(
            native_cli(
                repo,
                "claim",
                "release:T002",
                "--worktree",
                "--actor",
                "other-synthetic-owner",
                "--json",
            )
        )["data"]
        assert backend.get_claim(claimed["claim"]["id"]).status.value == "active"
        assert not (directory / "result.json").exists()
        native_cli(
            repo, "release", claimed["claim"]["id"], "--actor", "other-synthetic-owner", "--json"
        )
    finally:
        release.touch()
        for child in contenders:
            child.wait(timeout=30)
            assert process_group_absent(child.pid)
        if directory is not None:
            finished = _wait(directory)
    assert finished["state"] == "needs_review", finished
    runs = list((journal_root(repo) / "runs").glob("*/adapter.started.json"))
    assert len(runs) == 1


@pytest.mark.parametrize("mutation", ["release", "source"])
def test_intervening_owner_or_source_change_refuses_return(execution_project, tmp_path, mutation):
    from anvil.execution import native_cli

    backend, repo, _, _ = execution_project
    release = _hold_adapter(execution_project, tmp_path)
    preview = preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    admitted = confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / admitted["run_id"]
    try:
        _wait_adapter(directory)
        if mutation == "release":
            native_cli(
                Path(admitted["cwd"]),
                "release",
                admitted["claim_id"],
                "--actor",
                "synthetic-owner",
                "--json",
            )
        else:
            Path(admitted["cwd"]).joinpath("README.md").write_text("unrelated edit\n")
    finally:
        release.touch()
        finished = _wait(directory)
    assert finished["state"] == "reconciliation_required"
    assert finished["error_code"] == ("claim_changed" if mutation == "release" else "dirty_source")
    assert not (directory / "integration.json").exists()
    assert backend.get_latest_evidence("release:T001") is None


def test_existing_isolated_claim_runs_without_new_claim(execution_project):
    from anvil.execution import native_cli

    backend, repo, _, _ = execution_project
    claimed = json.loads(
        native_cli(
            repo, "claim", "release:T001", "--actor", "synthetic-owner", "--worktree", "--json"
        )
    )["data"]["claim"]
    isolated = Path(claimed["worktree_path"])
    with pytest.raises(ExecutionError, match="exact owner's isolated"):
        preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    preview = preview_run(isolated, "release:T001", "synthetic-owner", RunOptions())
    admitted = confirm_run(isolated, "release:T001", "synthetic-owner", preview["preview_id"])
    finished = _wait(journal_root(isolated) / "runs" / admitted["run_id"])
    assert finished["state"] == "needs_review", finished
    assert admitted["claim_id"] == claimed["id"]
    assert len([claim for claim in backend.list_claims() if claim.task_id == "release:T001"]) == 1


def test_partial_integration_keeps_first_edit_and_receipt(tmp_path, monkeypatch):
    from anvil.execution import digest

    root, output, journal = [tmp_path / name for name in ("root", "output", "journal")]
    root.mkdir()
    (output / "results" / "changed-files").mkdir(parents=True)
    journal.mkdir(mode=0o700)
    for name in ("one", "two"):
        (root / name).write_bytes(b"old")
        (output / "results" / "changed-files" / name).write_bytes(b"new")
    changes = [
        {"path": name, "before_sha256": digest(b"old"), "after_sha256": digest(b"new")}
        for name in ("one", "two")
    ]
    original_replace = os.replace

    def interrupted(source, destination):
        if destination.name == "two":
            raise OSError("simulated interrupted second integration")
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(OSError, match="second integration"):
        integrate(root, output, changes, {"one": b"old", "two": b"old"}, journal)
    assert (root / "one").read_bytes() == b"new"
    assert (root / "two").read_bytes() == b"old"
    assert (journal / "integration.json").is_file()
    assert (journal / "integrated-0.json").is_file()
    assert not (journal / "integrated-1.json").exists()


def test_native_failed_check_retains_edit_without_completion(execution_project):
    from tests.test_bundle_state import _event

    backend, repo, _, _ = execution_project
    task = backend.get_task("release:T001")
    command = "printf 'native-check-failed\\n'; exit 7"
    backend.append(
        _event(
            "task.created",
            {
                **task.model_dump(mode="json"),
                "prd_id": task.prd_id,
                "verification": {
                    "commands": [command],
                    "required_proofs": [
                        {
                            "kind": "command",
                            "command": command,
                            "passing_exit_codes": [0],
                            "label": "failure",
                        }
                    ],
                },
            },
            target_kind="task",
            target_id=task.id,
        )
    )
    preview = preview_run(repo, task.id, "synthetic-owner", RunOptions())
    admitted = confirm_run(repo, task.id, "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / admitted["run_id"]
    result = _wait(directory)
    assert result["state"] == "reconciliation_required"
    assert result["error_code"] == "command_failed"
    assert Path(admitted["cwd"]).joinpath("README.md").read_bytes() == b"implemented\n"
    assert (directory / "check-0.log").read_bytes() == b"native-check-failed\n"
    assert not list(directory.glob("proof-*.json"))
    assert backend.get_latest_evidence(task.id) is None


def test_symlink_and_case_alias_integration_are_refused(tmp_path):
    from anvil.execution_runner import checked_path

    (tmp_path / "Outside").write_bytes(b"preserved")
    (tmp_path / "alias").symlink_to(tmp_path / "Outside")
    for name in ("alias", "outside"):
        with pytest.raises(ExecutionError, match="symbolic link|aliases"):
            checked_path(tmp_path, name)
    assert (tmp_path / "Outside").read_bytes() == b"preserved"


def test_async_capability_refresh_retains_real_process_without_claim(execution_project):
    from anvil.execution import read_execution_capabilities, refresh_execution_capabilities

    backend, repo, state, _ = execution_project
    before = (state / "events.jsonl").read_bytes()
    dispatched = refresh_execution_capabilities(repo)
    result = _wait(journal_root(repo) / "runs" / dispatched["run_id"])
    assert result["state"] == "capabilities_ready", result
    capabilities = read_execution_capabilities(repo)
    assert capabilities["status"] == "fresh"
    assert capabilities["models"] == [{"provider": "synthetic", "id": "model"}]
    assert capabilities["thinking"]["model"] == "model"
    assert not backend.list_active_claims()
    assert (state / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize("bad_return", ["wrong_options", "wrong_preimage"])
def test_return_cannot_change_admitted_options_or_preimages(execution_project, bad_return):
    import dataclasses

    import yaml

    from anvil.config import global_config_path
    from anvil.execution import digest

    backend, repo, _, registration = execution_project
    Path(registration.profile).write_text(json.dumps({bad_return: True}))
    value = dataclasses.asdict(registration)
    value["hashes"]["profile"] = digest(Path(registration.profile).read_bytes())
    global_config_path().write_text(yaml.safe_dump({"execution_adapter": value}))
    preview = preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    admitted = confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / admitted["run_id"]
    result = _wait(directory)
    assert result["state"] == "reconciliation_required"
    assert result["error_code"] in {"result_mismatch", "invalid_changes"}
    assert not (directory / "integration.json").exists()
    assert Path(admitted["cwd"]).joinpath("README.md").read_bytes() == b"initial\n"
    assert backend.get_latest_evidence("release:T001") is None
