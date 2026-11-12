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
    _assert_portable_contracts(directory)

    # Repeating the consumed preview must preserve the completed slot and receipts.
    before = {
        str(path.relative_to(journal_root(repo))): path.read_bytes()
        for path in journal_root(repo).rglob("*")
        if path.is_file()
    }
    with pytest.raises(ExecutionError, match="already dispatched"):
        confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    after = {
        str(path.relative_to(journal_root(repo))): path.read_bytes()
        for path in journal_root(repo).rglob("*")
        if path.is_file()
    }
    assert after == before

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
            [sys.executable, *command],
            tmp_path,
            tmp_path,
            "check",
            time.monotonic() + 0.5,
            131072,
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
    data["registration"]["profile_sha256"] = digest(Path(registration.profile).read_bytes())
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
            repo,
            "release",
            claimed["claim"]["id"],
            "--actor",
            "other-synthetic-owner",
            "--json",
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
            repo,
            "claim",
            "release:T001",
            "--actor",
            "synthetic-owner",
            "--worktree",
            "--json",
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
    assert result["state"] == "failed_stopped"
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
    from anvil.execution import (
        read_execution_capabilities,
        refresh_execution_capabilities,
    )

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
    value["registration"]["profile_sha256"] = digest(Path(registration.profile).read_bytes())
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


def _assert_portable_contracts(directory):
    import copy
    import dataclasses

    from anvil.config import ExecutionAdapter
    from anvil.execution import AdapterResult, ExecutionRequest, digest, json_bytes
    from anvil.state.hashing import canonical_json_bytes

    request_data = read_receipt(directory / "request.json")
    returned_data = read_receipt(directory / "adapter-result.json")
    request = ExecutionRequest.from_data(request_data)
    assert dataclasses.asdict(request) == request_data
    assert dataclasses.asdict(AdapterResult.from_data(returned_data, request)) == returned_data
    assert request.packet_sha256 == digest((directory / "packet.txt").read_bytes())
    assert request.assignment_sha256 == digest((directory / "adapter-task.json").read_bytes())
    registration = read_receipt(directory / "context.json")["adapter"]
    assert request.adapter["registration_sha256"] == digest(
        json_bytes(registration["registration"])
    )
    assert (
        request.writable_paths[0]["host_mode"]
        == (Path(request.source["claim_worktree"]) / "README.md").stat().st_mode & 0o777
    )
    assert returned_data["custody"]["local_coordinator"].startswith("pid:")
    assert returned_data["custody"]["sandbox_id"] == "synthetic-sandbox"
    assert returned_data["raw_receipt_sha256"] == digest(
        (directory / "adapter" / "receipt.json").read_bytes()
    )
    assert request.requested_options == {}
    assert json_bytes({"text": "café 雪"}) == canonical_json_bytes({"text": "café 雪"})
    assert b"\\u" not in json_bytes({"text": "café 雪"})

    # Rehash malformed requests so semantic guards, not just the digest, must refuse.
    request_mutations = [
        lambda value: value.update(unknown=True),
        lambda value: value.pop("packet_sha256"),
        lambda value: value["binding"].update(generation=True),
        lambda value: value["source"].update(cwd_identity="bad"),
        lambda value: value["writable_paths"][0].update(host_mode=0o4644),
        lambda value: value["budgets"].update(command_output_bytes=131073),
        lambda value: value["requested_options"].update(thinking=None),
        lambda value: value["requested_options"].update(thinking="auto"),
        lambda value: value.update(continuation={}),
        lambda value: value["verification"][0].update(timeout_seconds=601),
    ]
    for mutate in request_mutations:
        value = copy.deepcopy(request_data)
        mutate(value)
        value["request_sha256"] = digest(
            json_bytes({key: item for key, item in value.items() if key != "request_sha256"})
        )
        with pytest.raises(ExecutionError):
            ExecutionRequest.from_data(value)
    for field in ("request_sha256", "assignment_sha256", "source_commit"):
        value = copy.deepcopy(returned_data)
        value[field] = "0" * len(value[field])
        with pytest.raises(ExecutionError):
            AdapterResult.from_data(value, request)
    result_mutations = [
        lambda value: value.update(unknown=True),
        lambda value: value.pop("artifacts"),
        lambda value: value.update(acceptance="approved"),
        lambda value: value.update(saved_conversation={"session_id": "invented"}),
        lambda value: value["changes"][0].update(before_sha256="0" * 64),
        lambda value: value["artifacts"][0].update(size_bytes=True),
        lambda value: value["changes"][0].update(artifact_path="missing"),
        lambda value: value["custody"].update(stop_status="unknown"),
        lambda value: value["custody"].update(protected_unchanged=1),
        lambda value: value["custody"].update(receipt_sha256="0" * 64),
    ]
    for mutate in result_mutations:
        value = copy.deepcopy(returned_data)
        mutate(value)
        with pytest.raises(ExecutionError):
            AdapterResult.from_data(value, request)
    for mutation in (
        {"unknown": True},
        {"contract_version": True},
        {"code_sha256": {}},
        {"capabilities": {"run": True, "saved_session_continue": True}},
    ):
        value = copy.deepcopy(registration)
        value["registration"].update(mutation)
        with pytest.raises(ValueError):
            ExecutionAdapter(**value)


@pytest.mark.parametrize("failure", ["command_timeout", "output_overflow"])
def test_native_cli_uses_bounded_owned_capture(tmp_path, monkeypatch, failure):
    import anvil.execution as execution
    import anvil.execution_runner as runner

    monkeypatch.setattr(execution, "journal_root", lambda cwd: tmp_path)
    child_pid_file = tmp_path / "child.pid"
    # The synthetic native command owns and reaps its descendant on group TERM.
    program = """import os, pathlib, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
def stop(*args):
    child.wait(timeout=3)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
if sys.argv[2] == 'output_overflow':
    os.write(1, b'x' * 200000)
    os.write(2, b'y' * 200000)
time.sleep(60)
"""
    actual_capture = runner.capture

    def synthetic_native(argv, cwd, directory, name, deadline, maximum, environment, **options):
        assert argv[1:4] == ["-I", "-m", "anvil.cli"]
        assert options == {"separate_stdout": True, "require_success": False}
        return actual_capture(
            [sys.executable, "-c", program, str(child_pid_file), failure],
            cwd,
            directory,
            name,
            time.monotonic() + 1,
            maximum,
            environment,
            **options,
        )

    monkeypatch.setattr(runner, "capture", synthetic_native)
    with pytest.raises(ExecutionError) as error:
        execution.native_cli(tmp_path, "status", "--json", maximum=4096)
    assert error.value.code == failure
    (receipt_path,) = (tmp_path / "operations").glob("*/native.result.json")
    receipt = read_receipt(receipt_path)
    assert receipt["group_absent"] is True
    assert process_group_absent(receipt["pid"])
    assert receipt["output_bytes"] <= 4097
    assert receipt_path.with_name("native.log").stat().st_size <= 4097
    assert receipt_path.with_name("native.stdout").stat().st_size <= 4097
    with pytest.raises(ProcessLookupError):
        os.kill(int(child_pid_file.read_text()), 0)


def test_stopped_failed_run_allows_fresh_corrected_run(execution_project):
    import dataclasses

    import yaml

    from anvil.config import global_config_path
    from anvil.execution import digest, git

    backend, repo, _, registration = execution_project
    profile = Path(registration.profile)
    profile.write_text(json.dumps({"returned_content": "needs correction\n"}))
    owner = dataclasses.asdict(registration)
    owner["registration"]["profile_sha256"] = digest(profile.read_bytes())
    global_config_path().write_text(yaml.safe_dump({"execution_adapter": owner}))
    preview = preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    first = confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / first["run_id"]
    assert _wait(directory)["state"] == "failed_stopped"
    old_result = (directory / "result.json").read_bytes()
    old_output = (directory / "check-0.log").read_bytes()
    isolated = Path(first["cwd"])
    # Explicit synthetic owner action: preserve returned work in its own commit.
    git(isolated, "add", "README.md")
    git(isolated, "commit", "-m", "Preserve failed synthetic attempt for correction")
    profile.write_text("{}")
    owner["registration"]["profile_sha256"] = digest(profile.read_bytes())
    global_config_path().write_text(yaml.safe_dump({"execution_adapter": owner}))
    next_preview = preview_run(isolated, "release:T001", "synthetic-owner", RunOptions())
    second = confirm_run(isolated, "release:T001", "synthetic-owner", next_preview["preview_id"])
    assert _wait(journal_root(repo) / "runs" / second["run_id"])["state"] == "needs_review"
    assert second["claim_id"] == first["claim_id"]
    assert (directory / "result.json").read_bytes() == old_result
    assert (directory / "check-0.log").read_bytes() == old_output
    assert backend.get_latest_evidence("release:T001") is not None


@pytest.mark.parametrize("mutation", ["custody", "artifact"])
def test_forged_portable_return_is_refused_before_submission(execution_project, tmp_path, mutation):
    import shlex

    from anvil.execution import json_bytes
    from tests.test_bundle_state import _event

    backend, repo, _, _ = execution_project
    released = tmp_path / "finish-check"
    ready = tmp_path / "check-ready"
    loop = f"while not pathlib.Path({str(released)!r}).exists(): time.sleep(.02)"
    code = "import pathlib; import time; " + f"pathlib.Path({str(ready)!r}).touch(); exec({loop!r})"
    command = shlex.join([sys.executable, "-c", code])
    task = backend.get_task("release:T001")
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
                            "label": "held",
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
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            if (directory / "result.json").exists():
                pytest.fail(str(read_receipt(directory / "result.json")))
            time.sleep(0.02)
        assert ready.exists()
        # Simulate an adversary replacing local journal evidence while the
        # actual check is running; the worker must rejoin actual return bytes.
        if mutation == "custody":
            path = directory / "adapter-result.json"
            returned = read_receipt(path)
            returned["custody"]["sandbox_id"] = "another-owner"
            path.write_bytes(json_bytes(returned))
        else:
            (directory / "adapter" / "results" / "rpc-configuration.json").write_bytes(b"{}")
    finally:
        released.touch()
        finished = _wait(directory)
    assert finished["error_code"] == "result_mismatch", finished
    assert backend.get_latest_evidence(task.id) is None
    assert not (directory / "submission.json").exists()
