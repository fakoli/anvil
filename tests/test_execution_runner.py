from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
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
from tests.test_execution import saved_execution_project as saved_execution_project

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Owned Run adapter requires Linux descendant custody"
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


@pytest.mark.parametrize("preclaimed", [False, True])
def test_admission_repair_preserves_large_work_packet_and_current_proof(
    execution_project, preclaimed
):
    import signal

    from anvil.execution import native_cli
    from tests.test_bundle_state import _event

    backend, repo, _, _ = execution_project
    task = backend.get_task("release:T001")
    description = "x" * 28000
    backend.append(_event("task.created", {
        **task.model_dump(mode="json"), "prd_id": task.prd_id, "description": description,
    }, target_kind="task", target_id=task.id))
    directory = repo
    if preclaimed:
        claimed = json.loads(native_cli(
            repo, "claim", task.id, "--actor", "synthetic-owner", "--worktree", "--json"
        ))["data"]["claim"]
        directory = Path(claimed["worktree_path"])
    preview = preview_run(directory, task.id, "synthetic-owner", RunOptions())
    admitted = confirm_run(directory, task.id, "synthetic-owner", preview["preview_id"])
    retained = journal_root(repo) / "runs" / admitted["run_id"]
    try:
        result = _wait(retained)
        assert result["state"] == "needs_review", result
        assignment = read_receipt(retained / "adapter-task.json")
        assert description in assignment["instructions"]
        assert 28000 <= len(assignment["instructions"].encode()) <= 32000
        assert "active_claim" not in assignment["instructions"]
        packet = (retained / "packet.txt").read_bytes()
        assert len(packet) > 32000
        assert json.loads(packet.split(b"\n\n", 1)[1])["active_claim"]["id"] == admitted["claim_id"]
        evidence = backend.get_latest_evidence(task.id)
        assert evidence.proofs[0].evidence_core.claim_id == admitted["claim_id"]
    finally:
        pid = read_receipt(retained / "launched.json")["pid"]
        if not process_group_absent(pid):
            os.killpg(pid, signal.SIGKILL)
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def test_admission_repair_unicode_new_owner_reaches_native_proof(execution_project):
    import signal

    backend, repo, _, _ = execution_project
    actor = "cafe\u0301"
    preview = preview_run(repo, "release:T001", actor, RunOptions())
    admitted = confirm_run(repo, "release:T001", actor, preview["preview_id"])
    retained = journal_root(repo) / "runs" / admitted["run_id"]
    try:
        result = _wait(retained)
        assert result["state"] == "needs_review", result
        claim = backend.get_claim(admitted["claim_id"])
        evidence = backend.get_latest_evidence("release:T001")
        assert claim.claimed_by == "caf\u00e9"
        assert evidence.proofs[0].evidence_core.claimed_by == claim.claimed_by
    finally:
        pid = read_receipt(retained / "launched.json")["pid"]
        if not process_group_absent(pid):
            os.killpg(pid, signal.SIGKILL)
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


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
    assert process_group_absent(result["supervisor_pid"])
    assert not Path(f"/proc/{result['pid']}").exists()
    assert (tmp_path / "check.log").exists()


@pytest.fixture
def adopted_test_children():
    """Own any escaped baseline descendant so a failing regression is cleaned up."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    try:
        yield
    finally:
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


def _stop_recorded_descendant(directory):
    import signal

    path = directory / "descendant.pid"
    if not path.exists():
        return
    pid = int(path.read_text())
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _detached_capture_command(detached, outcome):
    descendant = (
        "import os,signal,time;from pathlib import Path;"
        + ("os.setsid();" if detached else "")
        + "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "Path('descendant.pid').write_text(str(os.getpid()));time.sleep(60)"
    )
    return (
        "import subprocess,sys,time;from pathlib import Path;"
        f"subprocess.Popen([sys.executable,'-c',{descendant!r}]"
        + (",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
           if outcome != "inherited_pipe" else ")")
        + "\nwhile not Path('descendant.pid').exists():time.sleep(.005)\n"
        "print('captured-prefix',flush=True)\n"
        + {"success": "", "nonzero": "sys.exit(7)", "timeout": "time.sleep(60)",
           "overflow": "sys.stdout.write('x'*200000);sys.stdout.flush()",
           "inherited_pipe": ""}[outcome]
    )


def _owner_probe_candidate(tmp_path, outcome="success"):
    """Pinned synthetic owner with the real get/command/verify delegation shape."""
    import dataclasses

    from anvil.config import register_execution_adapter
    from anvil.execution import write_receipt

    tmp_path.chmod(0o700)
    script = tmp_path / "scripts" / "coordinate.py"
    script.parent.mkdir()
    script.write_text('''import json, subprocess
def validate_task(task): pass
def profile_settings(path): return json.loads(path.read_bytes())
def selected_state(state):
    return {key: state.get(key) for key in
            ("id", "name", "phase", "main_process_instance_id", "restart_count")}
class OpenShellOwner:
    def __init__(self, profile):
        self.profile, self.argv, self.main_instance = profile, profile["openshell_argv"], None
    def command(self, *args, timeout=60):
        return subprocess.run([*self.argv, "sandbox", *args], capture_output=True,
                              text=True, timeout=timeout, check=True)
    def get(self):
        return json.loads(self.command("get", self.profile["sandbox_name"], "-o", "json",
                                       timeout=30).stdout)
    def verify(self):
        state = self.get()
        if state["id"] != self.profile["sandbox_id"]:
            raise ValueError("Synthetic ownership identity changed")
        return state
''')
    extension = tmp_path / "extensions" / "coordination.js"
    extension.parent.mkdir()
    extension.write_text("// synthetic owner-only fixture\n")
    (tmp_path / "package.json").write_text(
        '{"name":"codex-pi-coordination","version":"0.3.0"}'
    )
    selected = {"id": "synthetic-id", "name": "synthetic-owner", "phase": "Stopped",
                "main_process_instance_id": "synthetic-main", "restart_count": 0}
    descendant = (
        "import os,signal,time;from pathlib import Path;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "Path('descendant.pid').write_text(str(os.getpid()));time.sleep(60)"
    )
    program = (
        "import subprocess,sys,time,json;from pathlib import Path;"
        "assert sys.argv[1:]==['sandbox','get','synthetic-owner','-o','json'];"
        f"subprocess.Popen([sys.executable,'-I','-c',{descendant!r}],"
        "start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "while not Path('descendant.pid').exists():time.sleep(.005)\n"
        + {"success": f"print({json.dumps({**selected, 'extra': 'omitted'})!r})",
           "overflow": "sys.stdout.write('x'*200000);sys.stdout.flush()",
           "timeout": "time.sleep(60)"}[outcome]
    )
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "sandbox_id": selected["id"], "sandbox_name": selected["name"],
        "openshell_argv": [sys.executable, "-I", "-c", program],
    }))
    registration = register_execution_adapter(script, Path(sys.executable), profile,
                                              saved_session_continue=True)
    candidate = tmp_path / "candidate.json"
    write_receipt(candidate, dataclasses.asdict(registration))
    return registration, candidate, selected


def test_candidate_owner_worker_uses_pinned_verifier_and_returns_only_selected_state(tmp_path):
    from anvil.execution import json_bytes

    _, candidate, selected = _owner_probe_candidate(tmp_path)
    output = tmp_path / "owner-cli"
    capture(
        [sys.executable, "-I", "-m", "anvil.execution_runner", "--verify-owner",
         "--candidate", str(candidate), "--output", str(output)],
        tmp_path, tmp_path, "owner", time.monotonic() + 10, 4096, separate_stdout=True,
    )
    assert (tmp_path / "owner.stdout").read_bytes() == json_bytes(selected)
    nested = read_receipt(output / "owner-get.result.json")
    assert nested["containment_complete"] is True
    assert nested["output_bytes"] < 65536
    assert not Path("/proc", (output / "descendant.pid").read_text()).exists()
    assert not list(tmp_path.rglob(".anvil"))


@pytest.mark.parametrize("outcome,code", [
    ("overflow", "output_overflow"), ("timeout", "command_timeout"),
])
def test_candidate_owner_inner_capture_bounds_raw_output_and_detached_children(
    tmp_path, monkeypatch, adopted_test_children, outcome, code
):
    from anvil import execution_runner

    registration, _, _ = _owner_probe_candidate(tmp_path, outcome)
    monkeypatch.setattr(execution_runner, "OWNER_READ_TIMEOUT_SECONDS", .75)
    output = tmp_path / "owner-cli"
    try:
        with pytest.raises(ExecutionError) as error:
            execution_runner.verify_candidate_owner(registration, output)
        assert error.value.code == code
        nested = read_receipt(output / "owner-get.result.json")
        assert nested["containment_complete"] is True
        assert nested["group_absent"] is True
        assert nested["output_bytes"] <= 65537
        assert not Path("/proc", (output / "descendant.pid").read_text()).exists()
    finally:
        _stop_recorded_descendant(output)


@pytest.mark.parametrize("mutation", ["foreign_id", "lifecycle_command"])
def test_candidate_owner_preserves_identity_check_and_refuses_other_commands(
    tmp_path, adopted_test_children, mutation
):
    from anvil.config import register_execution_adapter
    from anvil.execution_runner import verify_candidate_owner

    registration, _, _ = _owner_probe_candidate(tmp_path)
    script, profile = Path(registration.script), Path(registration.profile)
    if mutation == "foreign_id":
        data = json.loads(profile.read_bytes())
        data["sandbox_id"] = "different-configured-owner"
        profile.write_text(json.dumps(data))
    else:
        script.write_text(script.read_text().replace(
            'self.command("get", self.profile["sandbox_name"], "-o", "json",',
            'self.command("stop", self.profile["sandbox_name"], "-o", "json",',
        ))
    changed = register_execution_adapter(script, Path(sys.executable), profile,
                                         saved_session_continue=True)
    output = tmp_path / "owner-cli"
    try:
        with pytest.raises((ExecutionError, ValueError), match="ownership identity|another operation"):
            verify_candidate_owner(changed, output)
        if mutation == "lifecycle_command":
            assert list(output.iterdir()) == []
        else:
            assert read_receipt(output / "owner-get.result.json")["containment_complete"] is True
            assert not Path("/proc", (output / "descendant.pid").read_text()).exists()
    finally:
        _stop_recorded_descendant(output)


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("outcome", ["success", "nonzero", "timeout", "overflow", "inherited_pipe"])
def test_capture_reaps_all_descendants_before_return(
    tmp_path, adopted_test_children, detached, outcome
):
    try:
        options = (
            [sys.executable, "-c", _detached_capture_command(detached, outcome)],
            tmp_path, tmp_path, "contained", time.monotonic() + 1, 1024,
        )
        if outcome in {"success", "nonzero"}:
            result = capture(*options, require_success=False)
            assert result["exit_code"] == (0 if outcome == "success" else 7)
        else:
            with pytest.raises(ExecutionError):
                capture(*options)
            result = read_receipt(tmp_path / "contained.result.json")
        pid = int((tmp_path / "descendant.pid").read_text())
        assert not Path(f"/proc/{pid}").exists(), "capture returned before descendant reap"
        assert result["containment_complete"] is True
        assert result["group_absent"] is True
        assert result["pid"] != result["supervisor_pid"]
        assert result["supervisor_reaped"] is True
        assert (tmp_path / "contained.log").read_bytes().startswith(b"captured-prefix\n")
        assert result["output_bytes"] <= 1025
    finally:
        _stop_recorded_descendant(tmp_path)


def test_capture_parent_death_still_reaps_detached_descendants(tmp_path, adopted_test_children):
    import signal

    command = (
        "import os;from pathlib import Path;"
        "Path('command.pid').write_text(str(os.getpid()));"
        + _detached_capture_command(True, "timeout")
    )
    program = (
        "import sys,time;from pathlib import Path;"
        "from anvil.execution_runner import capture;"
        "directory=Path(sys.argv[1]);"
        f"capture([sys.executable,'-c',{command!r}],directory,directory,'parent',"
        "time.monotonic()+30,1024)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-I", "-c", program, str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    supervisor_pid = None
    try:
        limit = time.monotonic() + 5
        while not (tmp_path / "descendant.pid").exists():
            assert time.monotonic() < limit, "owned command did not start"
            time.sleep(.01)
        supervisor_pid = read_receipt(tmp_path / "parent.started.json")["supervisor_pid"]
        parent.kill()
        assert parent.wait(timeout=3) == -signal.SIGKILL
        limit = time.monotonic() + 5
        while True:
            waited, status = os.waitpid(supervisor_pid, os.WNOHANG)
            if waited:
                assert os.waitstatus_to_exitcode(status) == 0
                break
            assert time.monotonic() < limit, "orphaned supervisor did not clean up"
            time.sleep(.01)
        for name in ("command.pid", "descendant.pid"):
            assert not Path("/proc", (tmp_path / name).read_text()).exists()
        assert not (tmp_path / "parent.result.json").exists()
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
        if supervisor_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(supervisor_pid, signal.SIGTERM)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(supervisor_pid, 0)
        _stop_recorded_descendant(tmp_path)


def test_capture_preserves_actual_signed_command_exit(tmp_path):
    result = capture(
        [sys.executable, "-c", "import os,signal;os.kill(os.getpid(),signal.SIGTERM)"],
        tmp_path, tmp_path, "signal", time.monotonic() + 3, 1024, require_success=False,
    )
    assert result["exit_code"] == -signal.SIGTERM
    assert result["supervisor_exit_code"] == 0
    assert result["command_reaped"] and result["containment_complete"]


def test_capture_refuses_ignored_child_wait_status_before_start(tmp_path):
    previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        with pytest.raises(ExecutionError, match="observable child wait status"):
            capture(
                [sys.executable, "-c", "from pathlib import Path;Path('started').touch()"],
                tmp_path, tmp_path, "ignored", time.monotonic() + 3, 1024,
            )
    finally:
        signal.signal(signal.SIGCHLD, previous)
    assert not list(tmp_path.iterdir())


def test_capture_group_observation_failure_retains_unknown_custody(tmp_path, monkeypatch):
    import anvil.execution_runner as runner

    def denied(pid):
        raise PermissionError("synthetic group observation denial")

    monkeypatch.setattr(runner, "process_group_absent", denied)
    with pytest.raises(ExecutionError) as error:
        capture(
            [sys.executable, "-c", "print('retained-output')"],
            tmp_path, tmp_path, "unknown", time.monotonic() + 3, 1024,
        )
    assert error.value.code == "containment_unknown"
    result = read_receipt(tmp_path / "unknown.result.json")
    assert result["group_absent"] is False and result["containment_complete"] is False
    assert result["supervisor_reaped"] is True
    assert (tmp_path / "unknown.log").read_bytes() == b"retained-output\n"


def test_missing_current_capture_result_cannot_retire_run(execution_project, monkeypatch):
    import anvil.execution as execution

    backend, repo, _, _ = execution_project
    original = execution.subprocess.Popen
    worker_fault = """import sys
from anvil import execution_runner as runner
original = runner.write_receipt
def fail_current_result(path, value):
    if path.name == 'check-0.result.json':
        raise OSError('synthetic current custody result write failure')
    return original(path, value)
runner.write_receipt = fail_current_result
raise SystemExit(runner.main())
"""

    def launch_real_worker_with_storage_fault(argv, *args, **kwargs):
        if argv[1:4] == ["-I", "-m", "anvil.execution_runner"]:
            assert kwargs["start_new_session"] is True
            argv = [argv[0], "-I", "-c", worker_fault, argv[4]]
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(execution.subprocess, "Popen", launch_real_worker_with_storage_fault)
    preview = preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    admitted = confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / admitted["run_id"]
    result = _wait(directory)
    assert result["state"] == "reconciliation_required", result
    assert result["local_children_stopped"] is False
    assert not (directory / "check-0.result.json").exists()
    assert (directory / "check-0.log").read_bytes() == b"native-proof-ok\n"
    assert not list(directory.glob("proof-*.json"))
    assert backend.get_latest_evidence("release:T001") is None


@pytest.mark.parametrize("changed", [
    "malformed_result", "foreign_started", "boolean_exit", "duplicate_attempt",
])
def test_stopped_capture_requires_matching_complete_receipts(tmp_path, changed):
    from anvil.execution_runner import captures_stopped

    capture(
        [sys.executable, "-c", "print('verified')"],
        tmp_path, tmp_path, "check", time.monotonic() + 3, 1024,
    )
    assert captures_stopped(tmp_path, ["check"])
    if changed == "malformed_result":
        (tmp_path / "check.result.json").write_text("[false]")
    elif changed == "foreign_started":
        path = tmp_path / "check.started.json"
        value = read_receipt(path)
        value["supervisor_pid"] += 1
        path.write_text(json.dumps(value))
    elif changed == "boolean_exit":
        path = tmp_path / "check.result.json"
        value = read_receipt(path)
        value["supervisor_exit_code"] = False
        path.write_text(json.dumps(value))
    else:
        assert not captures_stopped(tmp_path, ["check", "check"])
        return
    assert not captures_stopped(tmp_path, ["check"])


@pytest.mark.parametrize("mode", ["detached_child", "lost_supervisor"])
def test_native_proof_requires_completed_descendant_containment(
    execution_project, tmp_path, adopted_test_children, mode
):
    import shlex

    from anvil.execution import git, reserve_slot
    from tests.test_bundle_state import _event

    backend, repo, _, _ = execution_project
    pid_file = tmp_path / "verification-descendant.pid"
    if mode == "detached_child":
        child = (
            "import os,signal,time;from pathlib import Path;os.setsid();"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()));time.sleep(60)"
        )
        program = (
            "import subprocess,sys,time;from pathlib import Path;"
            f"subprocess.Popen([sys.executable,'-c',{child!r}],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            f"while not Path({str(pid_file)!r}).exists():time.sleep(.005)\n"
            "print('native-proof-ok')"
        )
    else:
        program = (
            "import os,signal;print('native-proof-ok',flush=True);"
            "os.kill(os.getppid(),signal.SIGKILL)"
        )
    (repo / "containment_check.py").write_text(program)
    git(repo, "add", "containment_check.py")
    git(repo, "commit", "-qm", "Synthetic frozen containment check")
    command = "exec " + shlex.join([sys.executable, "containment_check.py"])
    task = backend.get_task("release:T001")
    backend.append(_event("task.created", {
        **task.model_dump(mode="json"), "prd_id": task.prd_id,
        "verification": {"commands": [command], "required_proofs": [
            {"kind": "command", "command": command, "passing_exit_codes": [0],
             "label": "contained-native-check"},
        ]},
    }, target_kind="task", target_id=task.id))
    preview = preview_run(repo, task.id, "synthetic-owner", RunOptions())
    admitted = confirm_run(repo, task.id, "synthetic-owner", preview["preview_id"])
    root = journal_root(repo)
    directory = root / "runs" / admitted["run_id"]
    command_pid = None
    try:
        result = _wait(directory)
        captured = read_receipt(directory / "check-0.result.json")
        command_pid = captured["pid"]
        if mode == "detached_child":
            assert result["state"] == "needs_review", result
            assert captured["containment_complete"] is True
            assert not Path("/proc", pid_file.read_text()).exists()
            assert backend.get_latest_evidence(task.id).proofs[0].evidence_core.exit_code == 0
        else:
            assert result["state"] == "reconciliation_required", result
            assert result["local_children_stopped"] is False
            assert captured["containment_complete"] is False
            assert captured["exit_code"] is None  # Never substitute supervisor's exit.
            assert not list(directory.glob("proof-*.json"))
            assert backend.get_latest_evidence(task.id) is None
            with pytest.raises(ExecutionError, match="requires inspection"):
                reserve_slot(root, next((root / "slots").glob("*.json")), {
                    "run_id": "b" * 32, "task_id": task.id, "preview_id": "c" * 32,
                })
    finally:
        if command_pid is not None:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(command_pid, 0)
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(int(pid_file.read_text()), 0)


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
child = subprocess.Popen([sys.executable, '-c',
    'import os,signal,time;os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
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
    assert process_group_absent(receipt["supervisor_pid"])
    assert not Path(f"/proc/{receipt['pid']}").exists()
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


def _set_synthetic_profile(project, **settings):
    import dataclasses

    import yaml

    from anvil.config import global_config_path
    from anvil.execution import digest

    registration = project[3]
    profile = Path(registration.profile)
    profile.write_text(json.dumps(settings))
    owner = dataclasses.asdict(registration)
    owner["registration"]["profile_sha256"] = digest(profile.read_bytes())
    global_config_path().write_text(yaml.safe_dump({"execution_adapter": owner}))


def test_default_run_ignores_unselectable_unrelated_catalog_entries(execution_project):
    from anvil.execution import read_execution_capabilities

    backend, repo, _, _ = execution_project
    _set_synthetic_profile(execution_project, mixed_catalog=True)
    preview = preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    admitted = confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / admitted["run_id"]
    finished = _wait(directory)
    assert finished["state"] == "needs_review", finished
    assert (
        backend.get_latest_evidence("release:T001").proofs[0].evidence_core.claim_id
        == admitted["claim_id"]
    )
    assert Path(admitted["cwd"]).joinpath("README.md").read_bytes() == b"implemented\n"
    capabilities = read_execution_capabilities(repo)
    assert capabilities["models"] == [{"provider": "synthetic", "id": "model"}]
    assert capabilities["omitted_models"] == 2
    assert capabilities["effective"] == {
        "provider": "synthetic",
        "model": "model",
        "thinking": "low",
    }
    assert len(list(journal_root(repo).glob("runs/*/adapter.started.json"))) == 1


@pytest.mark.parametrize(
    "bad_owner",
    ["missing", "foreign", "active", "main_mismatch", "missing_main", "protected", "cleanup"],
)
def test_metadata_requires_exact_stopped_owner_before_cache(execution_project, bad_owner):
    from anvil.execution import read_execution_capabilities, refresh_execution_capabilities

    backend, repo, state, _ = execution_project
    _set_synthetic_profile(execution_project, bad_owner=bad_owner)
    events = (state / "events.jsonl").read_bytes()
    dispatched = refresh_execution_capabilities(repo)
    directory = journal_root(repo) / "runs" / dispatched["run_id"]
    finished = _wait(directory)
    assert finished["state"] == "reconciliation_required", finished
    assert finished["local_children_stopped"] is False
    assert read_execution_capabilities(repo)["status"] == "unknown"
    assert not (journal_root(repo) / "capabilities" / "current.json").exists()
    assert (directory / "adapter" / "receipt.json").exists()
    assert not backend.list_active_claims()
    assert (state / "events.jsonl").read_bytes() == events


def test_default_run_survives_optional_cache_storage_failure(execution_project):
    backend, repo, _, _ = execution_project
    preview = preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    cache = journal_root(repo) / "capabilities"
    cache.write_bytes(b"synthetic storage obstruction")
    admitted = confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / admitted["run_id"]
    finished = _wait(directory)
    assert finished["state"] == "needs_review", finished
    assert finished["capability_cache"] == "unavailable"
    assert cache.read_bytes() == b"synthetic storage obstruction"
    assert (
        backend.get_latest_evidence("release:T001").proofs[0].evidence_core.claim_id
        == admitted["claim_id"]
    )


def test_metadata_projects_only_selectable_models(execution_project):
    from anvil.execution import read_execution_capabilities, refresh_execution_capabilities

    _, repo, _, _ = execution_project
    _set_synthetic_profile(execution_project, mixed_catalog=True)
    dispatched = refresh_execution_capabilities(repo)
    finished = _wait(journal_root(repo) / "runs" / dispatched["run_id"])
    assert finished["state"] == "capabilities_ready", finished
    capabilities = read_execution_capabilities(repo)
    assert capabilities["models"] == [{"provider": "synthetic", "id": "model"}]
    assert capabilities["omitted_models"] == 2


def test_run_rejects_unrepresentable_selected_model_before_integration(execution_project):
    backend, repo, _, _ = execution_project
    _set_synthetic_profile(execution_project, invalid_selected=True)
    preview = preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    admitted = confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / admitted["run_id"]
    finished = _wait(directory)
    assert finished["state"] == "reconciliation_required", finished
    assert finished["error_code"] in {"invalid_contract", "unsupported_options"}
    assert not (directory / "integration.json").exists()
    assert backend.get_latest_evidence("release:T001") is None
    assert Path(admitted["cwd"]).joinpath("README.md").read_bytes() == b"initial\n"


@pytest.mark.parametrize("bad_owner", ["missing", "main_mismatch"])
def test_run_requires_exact_stopped_owner_before_integration(execution_project, bad_owner):
    backend, repo, _, _ = execution_project
    _set_synthetic_profile(execution_project, bad_owner=bad_owner)
    preview = preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    admitted = confirm_run(repo, "release:T001", "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / admitted["run_id"]
    finished = _wait(directory)
    assert finished["state"] == "reconciliation_required", finished
    assert finished["error_code"] == "result_mismatch"
    assert not (directory / "integration.json").exists()
    assert backend.get_latest_evidence("release:T001") is None


def _saved_projection_inputs(tmp_path):
    """A verifier result double for the outer join, not a native proof fixture."""
    import io
    import tarfile
    from types import SimpleNamespace

    from anvil.execution import digest

    prior = {
        "conversation_id": "a" * 32,
        "prior_adapter_run_id": "b" * 32,
        "checkpoint_sha256": "c" * 64,
        "official_session_id": "synthetic-official-session",
        "session_sha256": "d" * 64,
    }
    before = {"bytes": 100, "sha256": prior["session_sha256"], "header_sha256": "e" * 64}
    after = {**before, "bytes": 140, "sha256": "f" * 64,
             "id": prior["official_session_id"], "file": "/private/session.jsonl",
             "cwd": "/private/workspace"}
    checkpoint = {
        "conversation_id": prior["conversation_id"],
        "prior_run_id": prior["prior_adapter_run_id"],
        "prior_checkpoint_sha256": prior["checkpoint_sha256"],
        "session": after,
        "workspace": {"files": {}, "directories": {".": 448}},
    }
    results = tmp_path / "results"
    results.mkdir()
    checkpoint_raw = (json.dumps(checkpoint, sort_keys=True) + "\n").encode()
    (results / "conversation.json").write_bytes(checkpoint_raw)
    native = {"version": 3, "operation": "continue"}
    with tarfile.open(tmp_path / "inputs.tar", "x") as archive:
        raw = json.dumps(native).encode()
        item = tarfile.TarInfo("task.json")
        item.size = len(raw)
        archive.addfile(item, io.BytesIO(raw))
    receipt = {
        "input_sha256": digest((tmp_path / "inputs.tar").read_bytes()),
        "worker": {"conversation": {
            "checkpoint_sha256": digest(checkpoint_raw),
            "session_before": before,
            "session_after": after,
            "prior_prefix_sha256": before["sha256"],
            "continuable": True,
        }},
    }
    # The trusted Pi verifier is independently exercised by the concrete adapter;
    # this unit test targets the native prior-dispatch join after that verifier.
    adapter = SimpleNamespace(verify_conversation=lambda *_: checkpoint)
    request = SimpleNamespace(operation="continue", continuation=prior)
    return adapter, request, receipt, checkpoint


def test_saved_projection_omits_remote_paths_and_joins_actual_checkpoint(tmp_path):
    from anvil.execution import digest, json_bytes
    from anvil.execution_runner import verified_saved_conversation

    adapter, request, receipt, checkpoint = _saved_projection_inputs(tmp_path)
    result = verified_saved_conversation(adapter, tmp_path, request, receipt)
    assert result["official_session_id"] == request.continuation["official_session_id"]
    assert result["checkpoint_sha256"] == digest((tmp_path / "results/conversation.json").read_bytes())
    assert result["workspace_sha256"] == digest(json_bytes(checkpoint["workspace"]))
    assert set(result["session_after"]) == {"bytes", "sha256", "header_sha256"}
    assert "/private" not in json.dumps(result)


@pytest.mark.parametrize("mutation", [
    "checkpoint_bytes", "conversation_id", "prior_adapter_run_id", "checkpoint_sha256",
    "official_session_id", "session_sha256",
])
def test_saved_projection_refuses_prior_lineage_and_byte_drift(tmp_path, mutation):
    from anvil.execution_runner import verified_saved_conversation

    adapter, request, receipt, _ = _saved_projection_inputs(tmp_path)
    if mutation == "checkpoint_bytes":
        with (tmp_path / "results/conversation.json").open("ab") as stream:
            stream.write(b" ")
    else:
        request.continuation[mutation] = "0" * len(request.continuation[mutation])
    with pytest.raises(ExecutionError, match="checkpoint bytes|prior dispatch lineage"):
        verified_saved_conversation(adapter, tmp_path, request, receipt)


def test_saved_contract_probe_refuses_permissive_function_presence(tmp_path):
    from types import SimpleNamespace

    from anvil.execution_runner import validate_saved_adapter_contract

    def run_task(task, profile, output, lock, *, save_session=False,
                 previous_directory=None, selected_model=None):
        pytest.fail("contract validation must not execute a task")

    adapter = SimpleNamespace(
        run_task=run_task, continuation_checkpoint=lambda *_: None,
        verify_conversation=lambda *_: None, saved_descriptor=lambda *_: None,
        validate_native_task=lambda *_: None, exclusive_owner=lambda *_: None,
    )
    with pytest.raises(ExecutionError, match="accepts an invalid saved contract"):
        validate_saved_adapter_contract(adapter, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_saved_run_and_followup_continue_submit_distinct_native_proofs(saved_execution_project):
    import shlex

    from anvil.execution import confirm_continue, digest, git, preview_continue
    from tests.test_bundle_state import _event

    backend, repo, state, _ = saved_execution_project
    task = backend.get_task("release:T002")
    command = shlex.join([
        sys.executable, "-I", "-c",
        "from pathlib import Path; assert Path('README.md').read_bytes() == b'continued\\n'; "
        "print('followup-native-proof-ok')",
    ])
    backend.append(_event("task.created", {
        **task.model_dump(mode="json"), "prd_id": task.prd_id,
        "likely_files": ["README.md"],
        "verification": {"commands": [command], "required_proofs": [
            {"kind": "command", "command": command, "passing_exit_codes": [0],
             "label": "fresh-followup-proof"},
        ]},
    }, target_kind="task", target_id=task.id))
    first_preview = preview_run(repo, "release:T001", "synthetic-owner", RunOptions())
    first = confirm_run(repo, "release:T001", "synthetic-owner", first_preview["preview_id"])
    first_directory = journal_root(repo) / "runs" / first["run_id"]
    assert _wait(first_directory)["state"] == "needs_review"
    first_result = read_receipt(first_directory / "adapter-result.json")
    assert first_result["schema"] == "anvil.adapter-result.v2"
    assert first_result["saved_conversation"]["session_before"] is None
    first_source = Path(first["cwd"])
    git(first_source, "add", "README.md")
    git(first_source, "commit", "-qm", "Retain first synthetic returned edit")
    committed = git(first_source, "rev-parse", "HEAD").decode().strip()
    git(repo, "merge", "--ff-only", committed)

    second_preview = preview_continue(
        repo, first["run_id"], "synthetic-owner", RunOptions(), task_id="release:T002"
    )
    second = confirm_continue(
        repo, first["run_id"], "synthetic-owner", second_preview["preview_id"]
    )
    second_directory = journal_root(repo) / "runs" / second["run_id"]
    finished = _wait(second_directory)
    assert finished["state"] == "needs_review", finished
    returned = read_receipt(second_directory / "adapter-result.json")
    request = read_receipt(second_directory / "request.json")
    before, after = first_result["saved_conversation"], returned["saved_conversation"]
    assert request["operation"] == "continue"
    assert request["source"]["commit"] == committed
    assert request["continuation"]["prior_run_id"] == first["run_id"]
    assert request["continuation"]["prior_request_file_sha256"] == digest(
        (first_directory / "request.json").read_bytes()
    )
    assert after["official_session_id"] == before["official_session_id"]
    assert after["conversation_id"] == before["conversation_id"]
    assert after["session_before"] == before["session_after"]
    assert after["prior_prefix_sha256"] == before["session_after"]["sha256"]
    assert after["prior_checkpoint_sha256"] == before["checkpoint_sha256"]
    assert after["checkpoint_sha256"] != before["checkpoint_sha256"]
    assert after["session_after"]["bytes"] > after["session_before"]["bytes"]
    assert first["claim_id"] != second["claim_id"] and first["run_id"] != second["run_id"]
    evidence = [backend.get_latest_evidence(name) for name in ("release:T001", "release:T002")]
    for current, admitted, task_id in zip(
        evidence, (first, second), ("release:T001", "release:T002"), strict=True
    ):
        proof = current.proofs[0].evidence_core
        assert proof.claim_id == admitted["claim_id"] and proof.task_id == task_id
        assert proof.generation == 1  # Independent tasks need not share a generation counter.
        assert backend.get_claim(admitted["claim_id"]).status.value == "released"
        assert backend.get_task(task_id).status.value == "needs_review"
    assert evidence[0].proofs[0] != evidence[1].proofs[0]
    assert Path(second["cwd"]).joinpath("README.md").read_bytes() == b"continued\n"
    assert not any(json.loads(line)["action"] == "task.applied"
                   for line in (state / "events.jsonl").read_bytes().splitlines())
    assert len(list(journal_root(repo).glob("runs/*/adapter.started.json"))) == 2


@pytest.mark.parametrize("failure", ["command_failed", "output_overflow"])
def test_saved_native_check_failure_retains_conversation_without_submission(
    saved_execution_project, failure
):
    import shlex

    from tests.test_bundle_state import _event

    backend, repo, _, _ = saved_execution_project
    task = backend.get_task("release:T001")
    command = (
        "exit 9" if failure == "command_failed"
        else shlex.join([sys.executable, "-I", "-c", "print('x' * 200000)"])
    )
    backend.append(_event("task.created", {
        **task.model_dump(mode="json"), "prd_id": task.prd_id,
        "verification": {"commands": [command], "required_proofs": [
            {"kind": "command", "command": command, "passing_exit_codes": [0],
             "label": "deliberately-failing-native-check"},
        ]},
    }, target_kind="task", target_id=task.id))
    preview = preview_run(repo, task.id, "synthetic-owner", RunOptions())
    admitted = confirm_run(repo, task.id, "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / admitted["run_id"]
    finished = _wait(directory)
    assert finished["state"] == "failed_stopped", finished
    assert finished["error_code"] == failure
    assert finished["local_children_stopped"] and finished["acceptance"] == "not_granted"
    assert read_receipt(directory / "adapter-result.json")["saved_conversation"]["continuable"]
    captured = read_receipt(directory / "check-0.result.json")
    if failure == "command_failed":
        assert captured["exit_code"] == 9
    else:
        assert captured["failure"] == "output_overflow" and captured["output_bytes"] == 131073
    assert not (directory / "submission.json").exists()
    assert backend.get_latest_evidence(task.id) is None
    assert backend.get_claim(admitted["claim_id"]).status.value == "active"


@pytest.mark.parametrize("exit_code", [0, 9])
def test_post_integration_source_drift_never_becomes_failed_stopped(
    saved_execution_project, exit_code
):
    import shlex

    from tests.test_bundle_state import _event

    backend, repo, _, _ = saved_execution_project
    task = backend.get_task("release:T001")
    command = shlex.join([
        sys.executable, "-I", "-c",
        "from pathlib import Path; import sys; "
        "Path('README.md').write_bytes(b'checker changed the integrated source\\n'); "
        f"sys.exit({exit_code})",
    ])
    backend.append(_event("task.created", {
        **task.model_dump(mode="json"), "prd_id": task.prd_id,
        "verification": {"commands": [command], "required_proofs": [
            {"kind": "command", "command": command, "passing_exit_codes": [0],
             "label": "source-drift-must-remain-held"},
        ]},
    }, target_kind="task", target_id=task.id))
    preview = preview_run(repo, task.id, "synthetic-owner", RunOptions())
    admitted = confirm_run(repo, task.id, "synthetic-owner", preview["preview_id"])
    directory = journal_root(repo) / "runs" / admitted["run_id"]
    finished = _wait(directory)
    assert read_receipt(directory / "check-0.result.json")["exit_code"] == exit_code
    assert finished["error_code"] == ("source_changed" if exit_code == 0 else "command_failed")
    assert finished["state"] == "reconciliation_required", finished
    assert finished["local_children_stopped"] is False
    assert finished["acceptance"] == "not_granted"
    assert not (directory / "submission.json").exists()
    assert backend.get_latest_evidence(task.id) is None
    assert backend.get_claim(admitted["claim_id"]).status.value == "active"
    assert Path(admitted["cwd"]).joinpath("README.md").read_bytes().startswith(b"checker changed")


def test_selected_metadata_reaches_native_variant_without_task_execution(saved_execution_project):
    from anvil.execution import read_execution_capabilities, refresh_execution_capabilities
    from anvil.execution_runner import retained_adapter_task

    backend, repo, state, _ = saved_execution_project
    before = (state / "events.jsonl").read_bytes()
    dispatched = refresh_execution_capabilities(repo, provider="synthetic", model="model")
    directory = journal_root(repo) / "runs" / dispatched["run_id"]
    finished = _wait(directory)
    assert finished["state"] == "capabilities_ready", finished
    receipt = read_receipt(directory / "adapter/receipt.json")
    native = retained_adapter_task(directory / "adapter", receipt)
    assert native["version"] == 3 and native["operation"] == "capabilities_selected"
    assert native["selected_model"] == {"provider": "synthetic", "model": "model"}
    assert receipt["worker"]["configuration"]["prompt_sent"] is False
    assert not native["checks"] and not native["writable_paths"]
    capabilities = read_execution_capabilities(repo)
    assert capabilities["thinking"]["provider"] == "synthetic"
    assert capabilities["thinking"]["model"] == "model"
    assert not backend.list_active_claims()
    assert (state / "events.jsonl").read_bytes() == before
