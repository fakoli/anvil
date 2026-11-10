"""The scale harness must prove its own accounting and retain failed captures."""

import base64
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from functools import partial
from pathlib import Path

import pytest
from benchmarks import execution_scale

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX qualification harness")


def test_real_command_captures_match_claims_and_replay(tmp_path):
    result = execution_scale.run(tmp_path / "scale", tasks=4, workers=2, cohort_size=2)
    assert result["validated"] and result["replay_equal"] and result["source_unchanged"]
    assert result["completed"] == result["peak_active_claims"] == 4
    assert result["ledger_writers"] == 1 and result["containers"] == 0
    assert result["event_counts"]["bundle.claimed"] == 2
    assert result["stages"]["claim_append"]["samples"] == 2
    assert result["stages"]["claim_append"]["max_ms"] <= result["stages"]["scoped_claim"]["max_ms"]
    assert result["event_counts"]["bundle.progress_noted"] == 4
    assert result["event_counts"]["evidence.submitted"] == 4
    assert result["web_snapshot"]["tasks"] == 4
    assert result["database_bytes"] == (tmp_path / "scale/project/.anvil/state.db").stat().st_size
    assert 0 < result["web_snapshot"]["output_bytes"] < result["web_snapshot"]["output_ceiling_bytes"]
    captures = list((tmp_path / "scale/captures").glob("*.proof.json"))
    assert len(captures) == 4
    assert len({json.loads(path.read_text())["payload"]["claim_id"] for path in captures}) == 4
    assert all(json.loads(path.read_text())["payload"]["exit_code"] == 0 for path in captures)


def test_nonzero_command_is_retained_but_cannot_become_completion(tmp_path, monkeypatch):
    seed = execution_scale._seed

    def failing_fixture(project, *arguments):
        result = seed(project, *arguments)
        with (project / "verify.py").open("a") as script:
            script.write("if sys.argv[1].endswith('T002.1'): raise SystemExit(7)\n")
        return result

    monkeypatch.setattr(execution_scale, "_seed", failing_fixture)
    result = execution_scale.run(tmp_path / "failed", tasks=3, workers=2, cohort_size=2)
    assert not result["validated"]
    assert result["replay_equal"] and result["no_false_acceptance"]
    assert result["completed"] == 2 and len(result["failures"]) == 1
    assert result["failures"][0]["task_id"] == "scale:T002.1"
    assert result["event_counts"]["evidence.submitted"] == 2
    failed = tmp_path / "failed/captures/scale:T002.1.proof.json"
    assert json.loads(failed.read_text())["payload"]["exit_code"] == 7
    assert (failed.parent / "scale:T002.1.output").is_file()


def test_executor_preparation_freezes_and_executes_the_same_command(tmp_path, monkeypatch):
    preparations = []

    def prepare(project):
        assert execution_scale._git(project, "show", "HEAD:verify.py")
        preparations.append(project)
        return lambda task_id: [sys.executable, "-I", "verify.py", task_id]

    # A reader launched from an agent session must still use the explicit fixture.
    monkeypatch.setenv("ANVIL_ROOT", str(tmp_path / "unrelated-project"))
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace")
    monkeypatch.setenv("ANVIL_PRD", "unrelated")
    monkeypatch.setenv("ANVIL_ACTOR", "unrelated")
    monkeypatch.setenv("ANVIL_CLAIM_ID", "unrelated")
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(tmp_path / "unrelated.toml"))
    result = execution_scale.run(tmp_path / "adapted", tasks=2, workers=2,
                                 prepare_executor=prepare)
    assert result["validated"] and len(preparations) == 1
    assert result["stages"]["executor_preparation"]["samples"] == 1
    for path in (tmp_path / "adapted/captures").glob("*.proof.json"):
        proof = json.loads(path.read_text())["payload"]
        command = base64.b64decode(proof["command_base64"]).decode()
        assert shlex.split(command) == [sys.executable, "-I", "verify.py", proof["task_id"]]
    assert not (tmp_path / "unrelated-project").exists()


def _stalled_child(output, options):
    os.setsid()
    work = output.with_suffix(".work")
    work.mkdir()
    subprocess.Popen([
        sys.executable, "-c",
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(work / 'descendant.pid')!r}).write_text(str(os.getpid())); "
        "time.sleep(60)",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if options.get("interrupt_parent"):
        startup_deadline = time.monotonic() + 5
        while not (work / "descendant.pid").exists() and time.monotonic() < startup_deadline:
            time.sleep(.01)
        assert (work / "descendant.pid").exists()
        os.kill(os.getppid(), signal.SIGINT)
    time.sleep(60)


def _assert_descendant_stopped(output):
    pid = int((output.with_suffix(".work") / "descendant.pid").read_text())
    status = Path(f"/proc/{pid}/stat")
    # A reparented zombie may await init's reap, but must no longer execute.
    stopped = False
    for _ in range(40):
        try:
            process_state = status.read_text().split(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            stopped = True
            break
        if process_state == "Z":
            stopped = True
            break
        time.sleep(.05)
    if not stopped:
        os.kill(pid, signal.SIGKILL)
    assert stopped, "supervisor left a running command descendant"


def _require_process_group_control():
    try:
        os.killpg(os.getpgrp(), 0)
    except (AttributeError, OSError) as error:
        pytest.skip(f"POSIX process-group cleanup capability unavailable: {error!r}")


def test_denied_process_group_capability_refuses_before_constructing_a_child(tmp_path, monkeypatch):
    probes = []

    def deny_group_signal(process_group_id, signum):
        probes.append((process_group_id, signum))
        raise PermissionError("process-group signaling denied by sandbox")

    def unexpected_context(*_arguments, **_keywords):
        pytest.fail("capability refusal must occur before constructing a child")

    monkeypatch.setattr(execution_scale.os, "killpg", deny_group_signal)
    monkeypatch.setattr(execution_scale.multiprocessing, "get_context", unexpected_context)
    output = tmp_path / "unsupported.json"
    receipt = execution_scale._supervise(output, {}, 10)
    assert probes == [(os.getpgrp(), 0)]
    assert not receipt["validated"] and receipt["child_started"] is False
    assert receipt["child_stopped"] is None and receipt["child_exit_code"] is None
    assert receipt["error"]["code"] == "unsupported_environment"
    assert "denied by sandbox" in receipt["error"]["detail"]
    assert json.loads(output.read_text()) == receipt
    assert not output.with_suffix(".work").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc stopped-process observation")
def test_deadline_kills_term_ignoring_descendant_after_coordinator_exits(tmp_path, monkeypatch):
    _require_process_group_control()
    output = tmp_path / "timeout.json"
    supervise = execution_scale._supervise
    monkeypatch.setattr(execution_scale, "_supervise", partial(supervise, child_target=_stalled_child))
    monkeypatch.setattr(sys, "argv", ["execution_scale", "--tasks", "1", "--workers", "1",
                                     "--deadline", "10", "--output", str(output)])
    assert execution_scale.main() == 1
    receipt = json.loads(output.read_text())
    assert receipt["timed_out"] and not receipt["validated"] and receipt["child_stopped"]
    assert receipt["child_exit_code"] == -signal.SIGTERM
    _assert_descendant_stopped(output)
    assert json.loads(output.read_text())["timed_out"] is True


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc stopped-process observation")
def test_interrupt_stops_descendant_and_returns_a_failed_receipt(tmp_path):
    _require_process_group_control()
    output = tmp_path / "interrupted.json"
    previous_handlers = {signum: signal.getsignal(signum)
                         for signum in (signal.SIGINT, signal.SIGTERM)}
    receipt = execution_scale._supervise(
        output, {"interrupt_parent": True}, 10, child_target=_stalled_child,
    )
    assert receipt["interrupted"] and not receipt["timed_out"]
    assert not receipt["validated"] and receipt["child_stopped"]
    assert receipt["child_exit_code"] == -signal.SIGTERM
    _assert_descendant_stopped(output)
    assert json.loads(output.read_text()) == receipt
    assert {signum: signal.getsignal(signum) for signum in previous_handlers} == previous_handlers
