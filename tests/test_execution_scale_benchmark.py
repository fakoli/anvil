"""The scale harness must prove its own accounting and retain failed captures."""

import base64
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from pathlib import Path

import pytest
from benchmarks import execution_scale

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX qualification harness")


@pytest.mark.parametrize("mode", ["coordinator", "worker"])
def test_real_command_captures_match_claims_and_replay(tmp_path, mode):
    result = execution_scale.run(tmp_path / "scale", tasks=4, workers=2, cohort_size=2,
                                 proof_preparation=mode)
    assert result["validated"] and result["replay_equal"] and result["source_unchanged"], (
        result["failures"])
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
    assert result["proof_preparation"] == mode
    assert result["attempted"] == result["command_captures"] == 4
    assert result["failed_tasks"] == 0 and result["retained_proof_artifacts"] == 4
    for stage, samples in result["raw_stage_seconds"].items():
        assert result["stages"][stage] == execution_scale._summary(samples)
    rows = result["raw_timings"]
    assert len(rows) == 4 and {row["task_id"] for row in rows} == {
        json.loads(path.read_bytes())["payload"]["task_id"] for path in captures}
    for row in rows:
        proof_path = tmp_path / "scale/captures" / f"{row['task_id']}.proof.json"
        assert row["proof_sha256"] == hashlib.sha256(proof_path.read_bytes()).hexdigest()
        assert row["outcome"] == "submitted" and row["command_reaped"]
        assert (row["offered_monotonic"] <= row["started_monotonic"]
                <= row["ended_monotonic"] <= row["proof_started_monotonic"]
                <= row["proof_ready_monotonic"] <= row["writer_started_monotonic"]
                <= row["append_returned_monotonic"])
        assert row["ended_monotonic"] <= row["handled_monotonic"]
    assert result["stages"]["proof_queue"] == execution_scale._summary([
        row["handled_monotonic"] - row["ended_monotonic"] for row in rows])
    assert result["stages"]["command_end_to_durable_evidence"] == execution_scale._summary([
        row["append_returned_monotonic"] - row["ended_monotonic"] for row in rows])
    assert result["stages"]["offered_to_ledger"] == execution_scale._summary([
        row["append_returned_monotonic"] - row["offered_monotonic"] for row in rows])


@pytest.mark.parametrize("mode", ["coordinator", "worker"])
def test_nonzero_command_is_retained_but_cannot_become_completion(tmp_path, monkeypatch, mode):
    seed = execution_scale._seed

    def failing_fixture(project, *arguments):
        result = seed(project, *arguments)
        with (project / "verify.py").open("a") as script:
            script.write("if sys.argv[1].endswith('T002.1'): raise SystemExit(7)\n")
        return result

    monkeypatch.setattr(execution_scale, "_seed", failing_fixture)
    result = execution_scale.run(tmp_path / "failed", tasks=3, workers=2, cohort_size=2,
                                 proof_preparation=mode)
    assert not result["validated"]
    assert result["replay_equal"] and result["no_false_acceptance"]
    assert result["completed"] == 2 and len(result["failures"]) == 1
    assert result["failures"][0]["task_id"] == "scale:T002.1"
    assert result["event_counts"]["evidence.submitted"] == 2
    failed = tmp_path / "failed/captures/scale:T002.1.proof.json"
    assert json.loads(failed.read_text())["payload"]["exit_code"] == 7
    assert (failed.parent / "scale:T002.1.output").is_file()


@pytest.mark.parametrize("mode", ["coordinator", "worker"])
def test_executor_preparation_freezes_and_executes_the_same_command(tmp_path, monkeypatch, mode):
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
                                 prepare_executor=prepare, proof_preparation=mode)
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
    if options.get("interrupt_parent") or options.get("exit_after_startup"):
        startup_deadline = time.monotonic() + 5
        while not (work / "descendant.pid").exists() and time.monotonic() < startup_deadline:
            time.sleep(.01)
        assert (work / "descendant.pid").exists()
        if options.get("interrupt_parent"):
            os.kill(os.getppid(), signal.SIGINT)
        else:
            output.write_text(json.dumps({"validated": True}))
            return
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
@pytest.mark.parametrize("mode", ["coordinator", "worker"])
def test_deadline_kills_term_ignoring_descendant_after_coordinator_exits(tmp_path, monkeypatch, mode):
    _require_process_group_control()
    output = tmp_path / "timeout.json"
    supervise = execution_scale._supervise
    monkeypatch.setattr(execution_scale, "_supervise", partial(supervise, child_target=_stalled_child))
    monkeypatch.setattr(sys, "argv", ["execution_scale", "--tasks", "1", "--workers", "1",
                                     "--deadline", "10", "--proof-preparation", mode, "--output", str(output)])
    assert execution_scale.main() == 1
    receipt = json.loads(output.read_text())
    assert receipt["timed_out"] and not receipt["validated"] and receipt["child_stopped"]
    assert receipt["child_exit_code"] == -signal.SIGTERM
    _assert_descendant_stopped(output)
    assert json.loads(output.read_text())["timed_out"] is True


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc stopped-process observation")
@pytest.mark.parametrize("mode", ["coordinator", "worker"])
def test_interrupt_stops_descendant_and_returns_a_failed_receipt(tmp_path, mode):
    _require_process_group_control()
    output = tmp_path / "interrupted.json"
    previous_handlers = {signum: signal.getsignal(signum)
                         for signum in (signal.SIGINT, signal.SIGTERM)}
    receipt = execution_scale._supervise(
        output, {"interrupt_parent": True, "proof_preparation": mode}, 10, child_target=_stalled_child,
    )
    assert receipt["interrupted"] and not receipt["timed_out"]
    assert not receipt["validated"] and receipt["child_stopped"]
    assert receipt["child_exit_code"] == -signal.SIGTERM
    _assert_descendant_stopped(output)
    assert json.loads(output.read_text()) == receipt
    assert {signum: signal.getsignal(signum) for signum in previous_handlers} == previous_handlers


@pytest.mark.parametrize("mode", ["coordinator", "worker"])
def test_preparation_barrier_and_backend_thread_custody(tmp_path, monkeypatch, mode):
    """An owned real process releases preparation; no speed assertion is involved."""
    coordination = tmp_path / "barrier"
    coordination.mkdir()
    expected = 2 if mode == "worker" else 1
    controller = subprocess.Popen([
        sys.executable, "-I", "-c",
        "import sys,time; from pathlib import Path; p=Path(sys.argv[1]); "
        "limit=time.monotonic()+10; count=int(sys.argv[2])\n"
        "while len(list(p.glob('*.ready'))) < count:\n"
        " if time.monotonic()>limit: raise SystemExit(2)\n"
        " time.sleep(.01)\n"
        "(p/'release').write_text('released')\n",
        str(coordination), str(expected),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    original_proof = execution_scale._proof
    original_open = execution_scale._open
    coordinator_thread = threading.get_ident()
    proof_threads = set()
    thread_lock = threading.Lock()

    def checked_open(*args, **kwargs):
        backend = original_open(*args, **kwargs)
        for name in ("append", "get_task", "list_active_claims", "get_claim"):
            original = getattr(backend, name)

            def checked(*args, _original=original, **kwargs):
                assert threading.get_ident() == coordinator_thread
                return _original(*args, **kwargs)

            setattr(backend, name, checked)
        return backend

    def barrier_proof(project, task, claim, execution, **kwargs):
        with thread_lock:
            proof_threads.add(threading.get_ident())
        (coordination / f"{task.id}.ready").write_text(claim.id)
        deadline = time.monotonic() + 10
        while not (coordination / "release").exists():
            assert time.monotonic() < deadline, "preparation barrier was not released"
            time.sleep(.01)
        return original_proof(project, task, claim, execution, **kwargs)

    monkeypatch.setattr(execution_scale, "_open", checked_open)
    monkeypatch.setattr(execution_scale, "_proof", barrier_proof)
    try:
        result = execution_scale.run(tmp_path / "fixture", tasks=4, workers=2,
                                     cohort_size=2, proof_preparation=mode)
        assert result["validated"] and result["completed"] == 4
        assert controller.wait(timeout=5) == 0
        assert len(proof_threads) == expected
        assert (coordinator_thread in proof_threads) == (mode == "coordinator")
    finally:
        (coordination / "release").touch()
        if controller.poll() is None:
            controller.kill()
        controller.wait(timeout=5)


@pytest.mark.parametrize("mode", ["coordinator", "worker"])
@pytest.mark.parametrize("change", ["release", "expire"])
def test_native_writer_rechecks_custody_after_proof_ready(tmp_path, monkeypatch, mode, change):
    from anvil.clock import FrozenClock

    original_append = execution_scale._append
    intervened = []

    def append_after_intervention(backend, action, target_kind, target_id, payload):
        if action == "evidence.submitted" and not intervened:
            # The prevalidated proof has returned; mutate only on the coordinator thread.
            claim = backend.get_claim(payload["claim_id"])
            assert payload["proofs"][0]["evidence_core"]["claim_id"] == claim.id
            if change == "release":
                manager = execution_scale.BundleManager(
                    backend, execution_scale.SystemClock(), actor=execution_scale.ACTOR,
                    project_root=tmp_path / "fixture/project")
                manager.release("B00000", reason="Test barrier after proof preparation")
            else:
                backend._clock = FrozenClock(claim.lease_expires_at + timedelta(seconds=1))
            intervened.append(target_id)
        return original_append(backend, action, target_kind, target_id, payload)

    monkeypatch.setattr(execution_scale, "_append", append_after_intervention)
    result = execution_scale.run(tmp_path / "fixture", tasks=1, workers=1,
                                 proof_preparation=mode)
    assert intervened and not result["validated"], result["failures"]
    assert result["completed"] == 0 and result["replay_equal"]
    assert result["no_false_acceptance"] and len(result["failures"]) == 1
    row = result["raw_timings"][0]
    assert row["proof_ready_monotonic"] <= row["writer_started_monotonic"]
    assert "append_returned_monotonic" not in row and row["outcome"] == "failed"
    assert not result["stages"].get("command_end_to_durable_evidence")
    assert len(list((tmp_path / "fixture/captures").glob("*.proof.json"))) == 1


@pytest.mark.parametrize("mode", ["coordinator", "worker"])
@pytest.mark.parametrize("failure", ["overflow", "timeout", "malformed", "preparation"])
def test_failed_preparation_retains_bounded_diagnostics(tmp_path, monkeypatch, mode, failure):
    if failure in {"overflow", "timeout"}:
        def prepare(project):
            code = ("import sys; sys.stdout.write('x'*2000000)" if failure == "overflow"
                    else "import time; print('partial',flush=True); time.sleep(60)")
            return lambda task_id: [sys.executable, "-I", "-c", code, task_id]
    else:
        prepare = None
        if failure == "malformed":
            monkeypatch.setattr(execution_scale, "load_claim_command_proof",
                                lambda _raw: (_ for _ in ()).throw(ValueError("malformed proof")))
        else:
            monkeypatch.setattr(execution_scale, "_proof",
                                lambda *a, **k: (_ for _ in ()).throw(ValueError("preparation failed")))
    result = execution_scale.run(tmp_path / "fixture", tasks=1, workers=1,
                                 command_timeout=1, prepare_executor=prepare,
                                 proof_preparation=mode)
    assert not result["validated"] and result["completed"] == 0
    assert result["replay_equal"] and len(result["failures"]) == 1
    output = (tmp_path / "fixture/captures/scale:T001.output").read_bytes()
    assert len(output) <= execution_scale.MAX_COMMAND_OUTPUT_BYTES
    if failure == "overflow":
        assert len(output) == execution_scale.MAX_COMMAND_OUTPUT_BYTES
    if failure == "timeout":
        assert output == b"partial\n"
    row = result["raw_timings"][0]
    assert row["outcome"] == "failed" and row["command_reaped"]
    assert "append_returned_monotonic" not in row


def test_concurrent_artifact_budget_does_not_overbook_or_overwrite(tmp_path, monkeypatch):
    (tmp_path / "captures").mkdir()
    monkeypatch.setattr(execution_scale, "MAX_STORAGE_BYTES", 10)
    budget = execution_scale._ArtifactBudget(tmp_path)
    barrier = threading.Barrier(2)

    def write(index):
        barrier.wait(timeout=5)
        try:
            budget.write(tmp_path / "captures" / str(index), b"123456")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(write, range(2))) == [False, True]
    assert budget.refresh() == 6
    assert len(list((tmp_path / "captures").iterdir())) == 1


def test_invalid_mode_refuses_before_fixture_creation(tmp_path):
    with pytest.raises(ValueError):
        execution_scale.run(tmp_path / "absent", tasks=1, workers=1,
                            proof_preparation="unbounded")
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("mode", ["coordinator", "worker"])
def test_artifact_exhaustion_returns_failed_attempt_accounting(tmp_path, monkeypatch, mode):
    original_budget = execution_scale._ArtifactBudget

    class ExhaustedBudget(original_budget):
        def __init__(self, work):
            super().__init__(work)
            monkeypatch.setattr(execution_scale, "MAX_STORAGE_BYTES", self.other_bytes + 1)

    monkeypatch.setattr(execution_scale, "_ArtifactBudget", ExhaustedBudget)
    result = execution_scale.run(tmp_path / "fixture", tasks=2, workers=2,
                                 proof_preparation=mode)
    assert not result["validated"] and result["completed"] == 0
    assert result["attempted"] == result["failed_tasks"] == 2
    assert all(row["outcome"] == "failed" for row in result["raw_timings"])
    assert all("budget" in row["error"] for row in result["raw_timings"])
    assert not list((tmp_path / "fixture/captures").glob("*.proof.json"))
    assert result["replay_equal"] and result["no_false_acceptance"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc stopped-process observation")
@pytest.mark.parametrize("mode", ["coordinator", "worker"])
def test_normal_leader_exit_also_cleans_owned_descendants(tmp_path, mode):
    _require_process_group_control()
    output = tmp_path / "ordinary-exit.json"
    receipt = execution_scale._supervise(
        output, {"exit_after_startup": True, "proof_preparation": mode}, 10,
        child_target=_stalled_child,
    )
    assert receipt["child_exit_code"] == 0 and receipt["child_stopped"]
    _assert_descendant_stopped(output)
