"""Bounded execution/proof qualification on disposable, retained native State.

uv run --project bin python benchmarks/execution_scale.py --tasks 1000 --output /tmp/scale.json
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import math
import multiprocessing
import os
import selectors
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.conftest import append_exact_approved_prd  # noqa: E402

from anvil.bundles.execution import bundle_execution_scope_digest  # noqa: E402
from anvil.bundles.manager import BundleManager  # noqa: E402
from anvil.claims.command_proof_artifact import (  # noqa: E402
    claim_command_cwd_identity,
    load_claim_command_proof,
    verify_claim_command_proof_batch,
)
from anvil.clock import SystemClock  # noqa: E402
from anvil.review.gates import evidence_complete  # noqa: E402
from anvil.roots.registry import RootSetRegistry  # noqa: E402
from anvil.state.hashing import canonical_json_bytes  # noqa: E402
from anvil.state.models import Claim, EventDraft, Task  # noqa: E402
from anvil.state.schema import get_schema_version  # noqa: E402
from anvil.state.snapshot import serialize_state  # noqa: E402
from anvil.state.sqlite import SqliteBackend  # noqa: E402
from anvil.web import (  # noqa: E402
    MAX_OUTPUT_BYTES,
    MAX_SCRATCH_FILE_BYTES,
    READER_TIMEOUT_SECONDS,
)

ACTOR = "scale-coordinator"
PROJECT_ID = "execution-scale-fixture"
MAX_STORAGE_BYTES = 256 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 128 * 1024
NATIVE_STAGES = ("claim_append", "progress_append", "proof_validation", "evidence_append")


def _summary(samples):
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        **{
            name: round(ordered[math.ceil(len(ordered) * quantile) - 1] * 1000, 3)
            if ordered
            else None
            for name, quantile in (
                ("p50_ms", 0.5),
                ("p95_ms", 0.95),
                ("p99_ms", 0.99),
                ("max_ms", 1),
            )
        },
    }


def _append(backend, action, target_kind, target_id, payload):
    now = datetime.now(UTC)
    if action == "bundle.created":
        payload = {**payload, "created_at": now.isoformat(), "updated_at": now.isoformat()}
    return backend.append(
        EventDraft(
            timestamp=now,
            actor=ACTOR,
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            payload_json=payload,
        )
    )


def _open(project, durability="relaxed", events_storage="local"):
    state = project / ".anvil"
    state.mkdir(exist_ok=True)
    (state / "events.jsonl").touch()
    backend = SqliteBackend(
        db_path=str(state / "state.db"),
        events_path=str(state / "events.jsonl"),
        clock=SystemClock(),
        project_root=project,
        durability=durability,
        events_storage=events_storage,
    )
    backend.initialize()
    return backend


def _git(project, *arguments):
    return subprocess.run(
        ["git", "-C", str(project), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    ).stdout.strip()


def _native_command(task_id):
    return [sys.executable, "verify.py", task_id]


def _seed(project, backend, task_count, cohort_size, command_for_task=_native_command):
    now = datetime.now(UTC).isoformat()
    task_ids = ["scale:T001", *(f"scale:T002.{index}" for index in range(1, task_count))]
    _append(
        backend,
        "project.created",
        "project",
        PROJECT_ID,
        {
            "id": PROJECT_ID,
            "name": "Execution scale fixture",
            "description": "Synthetic",
            "created_at": now,
            "updated_at": now,
        },
    )
    append_exact_approved_prd(
        backend,
        timestamp=datetime.now(UTC),
        project_id=PROJECT_ID,
        prd_id="scale",
        title="Execution scale fixture",
        parsed_payload={
            "is_default": False,
            "summary": "Bounded qualification",
            "goals": [],
            "non_goals": [],
            "requirements": [
                {
                    "id": "scale:R001",
                    "prd_id": "scale",
                    "prd_section": "Requirements",
                    "text": "Run and retain every proof.",
                    "source_paragraph": None,
                    "derived": False,
                }
            ],
            "acceptance_criteria": [],
            "risks": [],
            "open_questions": [],
        },
    )
    _append(
        backend,
        "feature.created",
        "feature",
        "scale:F001",
        {
            "id": "scale:F001",
            "prd_id": "scale",
            "title": "Qualification",
            "description": "",
            "status": "ready",
            "requirements": ["scale:R001"],
            "tasks": task_ids,
        },
    )
    for task_id in task_ids:
        command = shlex.join(command_for_task(task_id))
        _append(
            backend,
            "task.created",
            "task",
            task_id,
            {
                "id": task_id,
                "feature_id": "scale:F001",
                "prd_id": "scale",
                "title": task_id,
                "description": "Run the deterministic fixture check.",
                "status": "ready",
                "priority": "high",
                "task_type": "feature",
                "dependencies": [],
                "conflict_groups": [],
                "likely_files": [],
                "verification": {
                    "commands": [command],
                    "required_proofs": [
                        {
                            "kind": "command",
                            "command": command,
                            "passing_exit_codes": [0],
                            "label": "Observed fixture command",
                        }
                    ],
                },
                "created_at": now,
                "updated_at": now,
            },
        )
    bundle_ids = []
    for index in range(0, task_count, cohort_size):
        bundle_id = f"B{index // cohort_size:05d}"
        bundle_ids.append(bundle_id)
        _append(
            backend,
            "bundle.created",
            "bundle",
            bundle_id,
            {
                "id": bundle_id,
                "schema_version": get_schema_version(),
                "prd_id": "scale",
                "task_ids": task_ids[index : index + cohort_size],
                "coordinator": ACTOR,
                "status": "planned",
                "review_policy": {},
                "throughput_budget": {"max_tasks": cohort_size, "max_serial_stages": 1},
                "created_at": now,
                "updated_at": now,
            },
        )
    return task_ids, bundle_ids


def _command(project, task_id, offered_at, command_timeout, command_argv):
    started = datetime.now(UTC)
    began = time.perf_counter()
    environment = {"PATH": os.environ.get("PATH", os.defpath), "PYTHONIOENCODING": "utf-8"}
    # Native executor CLIs locate their existing configuration through these
    # paths. Preserve their values without copying credential/environment files.
    environment.update(
        {key: os.environ[key] for key in ("HOME", "XDG_CONFIG_HOME") if key in os.environ}
    )
    output = bytearray()
    error = None
    # Commands stay in the supervisor's owned group, including descendants.
    # Never start a new session here: the outer deadline must still reach them.
    with subprocess.Popen(
        command_argv, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=environment,
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = command_timeout - (time.perf_counter() - began)
                    if remaining <= 0:
                        raise TimeoutError("Fixture command exceeded timeout")
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(
                            key.fd, min(65536, MAX_COMMAND_OUTPUT_BYTES + 1 - len(output))
                        )
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > MAX_COMMAND_OUTPUT_BYTES:
                            del output[MAX_COMMAND_OUTPUT_BYTES:]
                            raise ValueError(
                                "Fixture command exceeded output limit; retained prefix"
                            )
                process.wait(timeout=max(0.001, command_timeout - (time.perf_counter() - began)))
        except Exception as caught:
            error = repr(caught)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
    ended_monotonic = time.perf_counter()
    ended = datetime.now(UTC)
    return {
        "task_id": task_id,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "ended_at": ended.isoformat().replace("+00:00", "Z"),
        "exit_code": process.returncode,
        "output": bytes(output),
        "command_argv": command_argv,
        "queue_seconds": began - offered_at,
        "command_seconds": ended_monotonic - began,
        "offered_monotonic": offered_at,
        "started_monotonic": began,
        "ended_monotonic": ended_monotonic,
        "command_pid": process.pid,
        "command_reaped": process.returncode is not None,
        "error": error,
    }


class _ArtifactBudget:
    """Serialize capture reservations; only the coordinator accounts for State."""

    def __init__(self, work):
        self.work = work
        self.lock = threading.Lock()
        self.capture_bytes = 0
        self.exhausted = False
        self.other_bytes = 0
        self.refresh()

    def refresh(self):
        with self.lock:
            self.other_bytes = sum(
                path.stat().st_size for path in self.work.rglob("*")
                if path.is_file() and path.parent != self.work / "captures"
            )
            total = self.other_bytes + self.capture_bytes
            if total > MAX_STORAGE_BYTES:
                self.exhausted = True
                raise ValueError("Retained fixture exceeded 256 MiB storage budget")
            return total

    def write(self, path, raw):
        with self.lock:
            if (self.exhausted
                    or self.other_bytes + self.capture_bytes + len(raw) > MAX_STORAGE_BYTES):
                self.exhausted = True
                raise ValueError("Retained fixture exceeded 256 MiB storage budget")
            # Charge before opening; a failed/partial write never returns capacity.
            self.capture_bytes += len(raw)
            with path.open("xb") as stream:
                stream.write(raw)


def _prepare_proof(project, task_snapshot, claim_snapshot, execution, budget):
    execution["proof_started_monotonic"] = time.perf_counter()
    try:
        task = Task.model_validate_json(task_snapshot)
        claim = Claim.model_validate_json(claim_snapshot)
        raw, proof = _proof(project, task, claim, execution, artifact_writer=budget.write)
        execution["proof_sha256"] = hashlib.sha256(raw).hexdigest()
        execution["proof"] = proof
    except Exception as error:
        execution["error"] = repr(error)
    finally:
        proof_path = project.parent / "captures" / f"{execution['task_id']}.proof.json"
        try:
            if proof_path.exists():
                execution["proof_sha256"] = hashlib.sha256(proof_path.read_bytes()).hexdigest()
        except OSError as error:
            execution["error"] = repr(error)
        execution["proof_finished_monotonic"] = time.perf_counter()
    if execution["error"] is None:
        execution["proof_ready_monotonic"] = execution["proof_finished_monotonic"]


def _execute_task(project, task_id, offered_at, command_timeout, command_argv,
                  task_snapshot, claim_snapshot, budget, proof_preparation):
    execution = _command(project, task_id, offered_at, command_timeout, command_argv)
    try:
        budget.write(project.parent / "captures" / f"{task_id}.output", execution["output"])
        if proof_preparation == "worker" and execution["error"] is None:
            _prepare_proof(project, task_snapshot, claim_snapshot, execution, budget)
    except Exception as error:
        execution["error"] = repr(error)
    return execution


def _proof(project, task, claim, execution, *, artifact_writer=None):
    context = claim.attestation_context
    if context is None:
        raise ValueError("Native bundle member lacks immutable command-proof context")
    command = task.verification.commands[0]
    if shlex.join(execution["command_argv"]) != command:
        raise ValueError("Executed argv differs from the frozen task command")
    output = execution["output"]
    if len(output) > MAX_COMMAND_OUTPUT_BYTES:
        raise ValueError("Fixture command exceeded output limit")
    core = {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "claim_id": claim.id,
        "generation": claim.generation,
        "claimed_by": ACTOR,
        "task_id": task.id,
        "task_revision": context.task_revision,
        "prd_id": context.prd_id,
        "prd_revision": context.prd_revision,
        "repository_id": context.repository_id,
        "claim_start_sha": context.claim_start_sha,
        "cwd_relative": ".",
        "cwd_identity": claim_command_cwd_identity(project, context.repository_id, "."),
        "command_base64": base64.b64encode(command.encode()).decode(),
        "started_at": execution["started_at"],
        "ended_at": execution["ended_at"],
        "exit_code": execution["exit_code"],
        "output_base64": base64.b64encode(output).decode(),
        "output_sha256": hashlib.sha256(output).hexdigest(),
    }
    raw = canonical_json_bytes({"envelope_id": task.id, "payload": core, "issuer": None})
    proof_path = project.parent / "captures" / f"{task.id}.proof.json"
    if artifact_writer is None:
        proof_path.write_bytes(raw)
    else:
        artifact_writer(proof_path, raw)
    proof = verify_claim_command_proof_batch(
        (load_claim_command_proof(raw),),
        claim=claim,
        task=task,
        project_id=PROJECT_ID,
        project_root=project,
        actor=ACTOR,
        declared_commands=[command],
        now=datetime.now(UTC),
    )[0]
    return raw, proof


def _source_identity():
    checkout = Path(__file__).resolve().parents[1]
    files = [
        *sorted((checkout / "bin/src/anvil").rglob("*.py")),
        checkout / "tests/conftest.py",
        Path(__file__).resolve(),
    ]
    return {
        str(path.relative_to(checkout)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }


def _web_snapshot(project, work, source_identity, task_count):
    """Use the existing reader's scratch cap without escaping our process group."""
    script = work / "read-snapshot.py"
    script.write_text(
        "import hashlib,json,sys\n"
        "from pathlib import Path\n"
        "from anvil import project_snapshot,read_contracts\n"
        "from anvil.state import sqlite\n"
        "expected=json.loads(sys.argv[2])\n"
        "for module in (project_snapshot,read_contracts,sqlite):\n"
        " assert hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()=="
        "expected[module.__name__], 'isolated reader source differs'\n"
        "print(project_snapshot.read_project_snapshot(sys.argv[1]).model_dump_json())\n"
    )
    expected = {
        module: source_identity[f"bin/src/{module.replace('.', '/')}.py"]
        for module in ("anvil.project_snapshot", "anvil.read_contracts", "anvil.state.sqlite")
    }
    reader = [sys.executable, "-I", str(script), str(project / ".anvil"), json.dumps(expected)]
    output_path = work / "web-snapshot.json"
    error_path = work / "web-snapshot.stderr"
    with output_path.open("wb") as output, error_path.open("wb") as error:
        subprocess.run(
            [sys.executable, "-I", "-m", "anvil.web", "--reader", json.dumps(reader)],
            cwd=project,
            stdout=output,
            stderr=error,
            check=True,
            timeout=READER_TIMEOUT_SECONDS,
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "PYTHONIOENCODING": "utf-8",
                "ANVIL_STATE_LAYOUT": "local",
            },
        )
    output_bytes = output_path.stat().st_size
    if output_bytes > MAX_OUTPUT_BYTES:
        raise ValueError("Canonical reader exceeded the web output ceiling")
    snapshot = json.loads(output_path.read_bytes())
    assert len(snapshot["payload"]["tasks"]) == task_count
    return {
        "output_bytes": output_bytes,
        "tasks": task_count,
        "snapshot_digest": snapshot["snapshot_digest"],
        "scratch_ceiling_bytes": MAX_SCRATCH_FILE_BYTES,
        "output_ceiling_bytes": MAX_OUTPUT_BYTES,
    }


def _check_options(
    *,
    tasks=24,
    workers=8,
    cohort_size=12,
    command_timeout=10,
    durability="relaxed",
    events_storage="local",
    proof_preparation="coordinator",
):
    if not (
        1 <= tasks <= 2000
        and 1 <= workers <= min(tasks, 128)
        and 1 <= cohort_size <= 12
        and 1 <= command_timeout <= 30
        and durability in {"relaxed", "strict"}
        and events_storage in {"local", "git"}
        and proof_preparation in {"coordinator", "worker"}
    ):
        raise ValueError(
            "tasks=1..2000, workers=1..min(tasks,128), cohort=1..12, timeout=1..30; "
            "durability=relaxed|strict, events_storage=local|git, "
            "proof_preparation=coordinator|worker"
        )


def run(
    work,
    *,
    tasks=24,
    workers=8,
    cohort_size=12,
    command_timeout=10,
    durability="relaxed",
    events_storage="local",
    prepare_executor=None,
    proof_preparation="coordinator",
):
    """Run only below a fresh directory; the CLI additionally enforces a wall deadline."""
    _check_options(tasks=tasks, workers=workers, cohort_size=cohort_size,
                   command_timeout=command_timeout, durability=durability,
                   events_storage=events_storage, proof_preparation=proof_preparation)
    work.mkdir(parents=True, exist_ok=False)
    source_identity = _source_identity()
    project = work / "project"
    project.mkdir()
    captures = work / "captures"
    captures.mkdir()
    (project / "verify.py").write_text(
        "import json,sys,time\n"
        "task_id=sys.argv[1]\nnumber=int(task_id.rsplit('T',1)[1].replace('.',''))+10000\n"
        "assert sum(range(number+1))==number*(number+1)//2\n"
        "time.sleep(0.02)\nprint(json.dumps({'task_id':task_id,'verified':True}))\n"
    )
    _git(project, "init", "-q", "-b", "main")
    _git(project, "add", "verify.py")
    _git(
        project,
        "-c",
        "user.name=Scale fixture",
        "-c",
        "user.email=scale@example.invalid",
        "commit",
        "-qm",
        "Deterministic qualification command",
    )
    measurements = defaultdict(list)
    failures = []
    completed = []
    intervals = []
    raw_timings = []
    began = time.perf_counter()
    backend = _open(project, durability, events_storage)

    def timed(stage, function, *args, **kwargs):
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            measurements[stage].append(time.perf_counter() - started)

    native_append = backend.append

    def measured_append(event, *args, **kwargs):
        if event.action == "bundle.claimed":
            return timed("claim_append", native_append, event, *args, **kwargs)
        return native_append(event, *args, **kwargs)

    backend.append = measured_append

    def storage_guard():
        retained_bytes = sum(path.stat().st_size for path in work.rglob("*") if path.is_file())
        if retained_bytes > MAX_STORAGE_BYTES:
            raise ValueError("Retained fixture exceeded 256 MiB storage budget")
        return retained_bytes

    try:
        command_for_task = (
            timed("executor_preparation", prepare_executor, project)
            if prepare_executor is not None
            else _native_command
        )
        task_ids, bundle_ids = timed(
            "setup",
            _seed,
            project,
            backend,
            tasks,
            cohort_size,
            command_for_task,
        )
        manager = BundleManager(backend, SystemClock(), actor=ACTOR, project_root=project)
        # A separate fixture owner registry preserves the host's real root reservations.
        with RootSetRegistry(home=work / "owner").ordinary_claim_coordinator(project):
            for bundle_id in bundle_ids:
                scope = timed("scope_projection", bundle_execution_scope_digest, backend, bundle_id)
                timed("scoped_claim", manager.claim, bundle_id, expected_scope_digest=scope)
        claims = {claim.task_id: claim for claim in backend.list_active_claims()}
        assert set(claims) == set(task_ids)
        if any(claim.attestation_context is None for claim in claims.values()):
            raise ValueError("Native bundle member lacks immutable command-proof context")
        peak_active_claims = len(claims)
        for bundle_id in bundle_ids:
            bundle = backend.get_bundle(bundle_id)
            for task_id in bundle.task_ids:
                timed(
                    "progress_append",
                    manager.note_progress,
                    bundle_id,
                    phase="queued",
                    member_task_ids=[task_id],
                    detail="Awaiting a bounded executor slot",
                )
        storage_guard()
        budget = _ArtifactBudget(work)
        # Serialized snapshots are immutable and independent of live backend objects.
        task_snapshots = {}
        for task_id in task_ids:
            task = backend.get_task(task_id)
            # Task's public serializer deliberately excludes its internal PRD selector.
            task_snapshots[task_id] = json.dumps({**task.model_dump(mode="json"),
                                                  "prd_id": task.prd_id})
        claim_snapshots = {task_id: claim.model_dump_json() for task_id, claim in claims.items()}
        offered_at = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _execute_task, project, task_id, offered_at, command_timeout,
                    command_for_task(task_id), task_snapshots[task_id], claim_snapshots[task_id],
                    budget, proof_preparation,
                ): task_id for task_id in task_ids
            }
            for future in as_completed(futures):
                task_id = futures[future]
                row = {"task_id": task_id, "claim_id": claims[task_id].id,
                       "claim_generation": claims[task_id].generation,
                       "task_revision": claims[task_id].attestation_context.task_revision,
                       "proof_preparation": proof_preparation,
                       "offered_monotonic": offered_at, "outcome": "failed"}
                execution = None
                try:
                    execution = future.result()
                    execution["handled_monotonic"] = time.perf_counter()
                    intervals.extend(
                        [(execution["started_monotonic"], 1), (execution["ended_monotonic"], -1)]
                    )
                    for stage in ("queue", "command"):
                        measurements[stage].append(execution[f"{stage}_seconds"])
                    measurements["proof_queue"].append(
                        execution["handled_monotonic"] - execution["ended_monotonic"]
                    )
                    if proof_preparation == "coordinator" and execution["error"] is None:
                        _prepare_proof(project, task_snapshots[task_id], claim_snapshots[task_id],
                                       execution, budget)
                    if execution["error"] is not None:
                        raise ValueError(execution["error"])
                    if budget.exhausted:
                        raise ValueError("Retained fixture storage budget exhausted")
                    task = Task.model_validate_json(task_snapshots[task_id])
                    execution["writer_started_monotonic"] = time.perf_counter()
                    timed(
                        "evidence_append", _append, backend, "evidence.submitted", "task", task_id,
                        {"task_id": task_id, "claim_id": claims[task_id].id,
                         "submitted_by": ACTOR, "evidence_id": f"evidence-{task_id}",
                         "commands_run": task.verification.commands, "files_changed": [],
                         "proofs": [execution["proof"].model_dump(mode="json")]},
                    )
                    execution["append_returned_monotonic"] = time.perf_counter()
                    completed.append(task_id)
                    row["outcome"] = "submitted"
                    for stage, earlier in (
                        ("command_end_to_durable_evidence", "ended_monotonic"),
                        ("offered_to_ledger", "offered_monotonic"),
                    ):
                        measurements[stage].append(execution["append_returned_monotonic"]
                                                   - execution[earlier])
                except Exception as error:
                    row["error"] = repr(error)
                    failures.append({"task_id": task_id, "error": repr(error)})
                finally:
                    if execution is not None:
                        for stage, start, end in (
                            ("proof_validation", "proof_started_monotonic",
                             "proof_finished_monotonic"),
                            ("proof_preparation_wait", "ended_monotonic",
                             "proof_started_monotonic"),
                            ("proof_ready_to_writer", "proof_ready_monotonic",
                             "writer_started_monotonic"),
                        ):
                            if start in execution and end in execution:
                                measurements[stage].append(execution[end] - execution[start])
                        row.update({key: value for key, value in execution.items()
                                    if key.endswith("_monotonic") or key in (
                                        "started_at", "ended_at", "exit_code", "proof_sha256",
                                        "command_pid", "command_reaped")})
                        # Futures may remain until pool exit; retain artifacts, not duplicate bytes.
                        execution.pop("output", None)
                        execution.pop("proof", None)
                    raw_timings.append(row)
                    try:
                        budget.write(captures / f"{task_id}.timing.json",
                                     json.dumps(row, sort_keys=True).encode())
                    except Exception as error:
                        failures.append({"task_id": task_id, "stage": "timing_retention",
                                         "error": repr(error)})
                if len(raw_timings) % 16 == 0:
                    try:
                        budget.refresh()
                    except ValueError as error:
                        failures.append({"stage": "storage", "error": repr(error)})
        execution_seconds = time.perf_counter() - offered_at
        for bundle_id in bundle_ids:
            readiness = timed("bundle_readiness", manager.readiness, bundle_id)
            if not failures:
                assert readiness.can_mark_implemented, readiness
        evidence = backend.list_evidence()
        assert {item.task_id for item in evidence} == set(completed)
        assert len(evidence) == len(completed)
        for item in evidence:
            assert item.claim_id == claims[item.task_id].id
            assert evidence_complete(backend.get_task(item.task_id), item) == (True, [])
        for task in backend.list_tasks():
            if task.id in completed:
                assert task.status.value == "needs_review"
            else:
                assert task.status.value in {"claimed", "ready"}
        assert len(backend.list_active_claims()) <= len(failures)
        log = project / ".anvil" / "events.jsonl"
        events = [json.loads(line) for line in log.read_text().splitlines()]
        counts = Counter(event["action"] for event in events)
        assert counts["bundle.claimed"] == len(bundle_ids)
        assert counts["bundle.progress_noted"] == tasks
        assert counts["evidence.submitted"] == len(completed)
        assert counts["task.applied"] == 0
        before = serialize_state(backend)
        snapshot_bytes = len(json.dumps(before, sort_keys=True, separators=(",", ":")).encode())
        log_bytes = log.read_bytes()
        timed("replay", backend.replay_from_empty, str(log))
        assert serialize_state(backend) == before
        assert log.read_bytes() == log_bytes
        try:
            web_snapshot = timed(
                "web_snapshot", _web_snapshot, project, work, source_identity, tasks
            )
        except Exception as error:
            web_snapshot = {"error": repr(error)}
            failures.append({"stage": "web_snapshot", "error": repr(error)})
        lease_margin_seconds = min(
            (claim.lease_expires_at - datetime.now(UTC)).total_seconds()
            for claim in claims.values()
        )
    finally:
        backend.close()
    # Closing the sole writer checkpoints its WAL. Report the retained files,
    # not the small pre-checkpoint main file with most pages still in the WAL.
    try:
        retained_bytes = storage_guard()
    except ValueError as error:
        retained_bytes = sum(path.stat().st_size for path in work.rglob("*") if path.is_file())
        failures.append({"stage": "storage", "error": repr(error)})
    database_bytes = (project / ".anvil/state.db").stat().st_size
    active = peak_overlap = 0
    for _, change in sorted(intervals):
        active += change
        peak_overlap = max(peak_overlap, active)
    source_unchanged = source_identity == _source_identity()
    return {
        "schema": "anvil.execution-scale.v1",
        "observed_at": datetime.now(UTC).isoformat(),
        "state_schema": get_schema_version(),
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "durability": durability,
        "events_storage": events_storage,
        "proof_preparation": proof_preparation,
        "raw_timings": raw_timings,
        "source_sha256": source_identity,
        "source_unchanged": source_unchanged,
        "tasks": tasks,
        "cohort_size": cohort_size,
        "bundles": len(bundle_ids),
        "peak_active_claims": peak_active_claims,
        "executor_limit": workers,
        "observed_command_overlap": peak_overlap,
        "ledger_writers": 1,
        "containers": 0,
        "completed": len(completed),
        "attempted": len(raw_timings),
        "failed_tasks": sum(row["outcome"] == "failed" for row in raw_timings),
        "command_captures": sum("command_pid" in row for row in raw_timings),
        "retained_proof_artifacts": len(list(captures.glob("*.proof.json"))),
        "failures": failures,
        "event_counts": dict(counts),
        "stages": {stage: _summary(samples) for stage, samples in measurements.items()},
        "raw_stage_seconds": dict(measurements),
        "execution_seconds": round(execution_seconds, 3),
        "completed_per_second": round(len(completed) / execution_seconds, 3),
        "elapsed_seconds": round(time.perf_counter() - began, 3),
        "minimum_lease_margin_seconds": round(lease_margin_seconds, 3),
        "event_log_bytes": len(log_bytes),
        "database_bytes": database_bytes,
        "replay_oracle_snapshot_bytes": snapshot_bytes,
        "web_snapshot": web_snapshot,
        "retained_bytes": retained_bytes,
        "replay_equal": True,
        "no_false_acceptance": True,
        "validated": not failures and source_unchanged,
        "work_directory": str(work),
        "limits": [
            "Active claims, executor slots, ledger writers and containers are separate counts.",
            "One coordinator owns all mutations; the bounded command pool may also prepare proofs.",
            "Threaded preparation overlaps I/O, not necessarily Python CPU; native append "
            "still verifies current custody and proof material.",
            "Capture bytes are reserved under a lock; native State growth is checked every "
            "16 handled tasks and after close. Any exceeded storage budget fails the run.",
            "Command latency includes process startup and a 20ms fixture wait; "
            "queue delay is separate.",
            "Proofs are actual fixture-command captures, claim-owner self-attested, "
            "not external issuer attestations.",
            "No application correctness, browser, network-fleet, container "
            "or acceptance qualification.",
            "Native p95 target applies per declared stage; total execution has no 15ms guarantee.",
        ],
    }


def _child(output, options):
    os.setsid()
    try:
        receipt = run(output.with_suffix(".work"), **options)
    except BaseException as error:
        receipt = {
            "validated": False,
            "error": repr(error),
            "work_directory": str(output.with_suffix(".work")),
        }
    output.write_text(json.dumps(receipt, indent=2) + "\n")


def _stop_owned_process_group(process):
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        process.join(5)
        # The leader may exit on TERM while its children ignore it. Always
        # finish the owned group, even after the leader has been reaped.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.join(5)
        if process.is_alive():
            # Startup can stall before setsid establishes the owned group.
            process.kill()
            process.join(5)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _supervise(output, options, deadline, *, child_target=_child):
    try:
        # A signal-zero probe changes no process. Refuse before spawning if
        # the environment denies the group control required for bounded cleanup.
        os.killpg(os.getpgrp(), 0)
    except (AttributeError, OSError) as error:
        receipt = {
            "validated": False,
            "error": {
                "code": "unsupported_environment",
                "message": "POSIX process-group signaling is required for bounded cleanup.",
                "detail": repr(error),
            },
            "child_started": False,
            "child_stopped": None,
            "child_exit_code": None,
            "work_directory": str(output.with_suffix(".work")),
        }
        output.write_text(json.dumps(receipt, indent=2) + "\n")
        return receipt
    process = multiprocessing.get_context("spawn").Process(
        target=child_target, args=(output, options)
    )
    process.start()
    interrupted = False
    try:
        process.join(deadline)
    except KeyboardInterrupt:
        interrupted = True
    timed_out = not interrupted and process.is_alive()
    if timed_out or interrupted:
        _stop_owned_process_group(process)
        output.write_text(
            json.dumps(
                {
                    "validated": False,
                    "timed_out": timed_out,
                    "interrupted": interrupted,
                    "deadline_seconds": deadline,
                    "work_directory": str(output.with_suffix(".work")),
                },
                indent=2,
            )
            + "\n"
        )
    # Reap/stop any same-group command descendants even on an ordinary leader exit.
    if not (timed_out or interrupted):
        _stop_owned_process_group(process)
    if not output.exists():
        output.write_text(
            json.dumps({"validated": False, "child_exit_code": process.exitcode}) + "\n"
        )
    receipt = json.loads(output.read_text())
    receipt.update(
        child_started=True, child_exit_code=process.exitcode, child_stopped=not process.is_alive()
    )
    output.write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=24)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--cohort-size", type=int, default=12)
    parser.add_argument("--command-timeout", type=int, default=10)
    parser.add_argument("--durability", choices=("relaxed", "strict"), default="relaxed")
    parser.add_argument("--events-storage", choices=("local", "git"), default="local")
    parser.add_argument("--proof-preparation", choices=("coordinator", "worker"),
                        default="coordinator")
    parser.add_argument("--deadline", type=int, default=900)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("this bounded subprocess harness requires POSIX process-group cleanup")
    if not 10 <= args.deadline <= 3600:
        parser.error("deadline must be 10..3600 seconds")
    output = args.output.resolve()
    if output.exists() or output.with_suffix(".work").exists():
        parser.error("output and work directory must be new; prior evidence is preserved")
    output.parent.mkdir(parents=True, exist_ok=True)
    options = {
        name: getattr(args, name)
        for name in (
            "tasks",
            "workers",
            "cohort_size",
            "command_timeout",
            "durability",
            "events_storage",
            "proof_preparation",
        )
    }
    receipt = _supervise(output, options, args.deadline)
    print(
        json.dumps(
            {
                "output": str(output),
                "validated": receipt["validated"],
                "completed": receipt.get("completed"),
                "error": receipt.get("error"),
            }
        )
    )
    return 0 if receipt["validated"] and receipt["child_exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
