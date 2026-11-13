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
import queue
import re
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

from benchmarks.harness.engine import (  # noqa: E402
    _LINUX_SUBREAPER,
    PROCESS_CLEANUP_ALLOWANCE_SECONDS,
)
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


@contextlib.contextmanager
def _paced_results(task_ids, submit, workers, arrival_rate, epoch, arrival):
    """Offer independently of the sole consumer; slots include durable handling."""
    completions = queue.Queue(maxsize=len(task_ids))
    slots = threading.BoundedSemaphore(workers)
    stop = threading.Event()
    futures = []
    records = arrival["records"] = [
        {"task_id": task_id, "scheduled_offer_monotonic": epoch + index / arrival_rate,
         "admission": "not_offered"}
        for index, task_id in enumerate(task_ids)
    ]

    def produce():
        next_index = 0
        try:
            for index, record in enumerate(records):
                delay = max(0, record["scheduled_offer_monotonic"] - time.perf_counter())
                if stop.wait(delay):
                    break
                record["offered_monotonic"] = time.perf_counter()
                if not slots.acquire(blocking=False):
                    record["admission"] = "capacity_refused"
                    completions.put_nowait((None, record))
                    next_index = index + 1
                    continue
                record["reserved_monotonic"] = time.perf_counter()
                record["submitted_monotonic"] = time.perf_counter()
                try:
                    future = submit(record["task_id"], record["offered_monotonic"])
                except BaseException as error:
                    record.update(admission="submission_failed", error=repr(error))
                    completions.put_nowait((None, record))
                    next_index = index + 1
                    raise
                record["admission"] = "admitted"
                futures.append(future)
                future.add_done_callback(
                    lambda completed, record=record: completions.put_nowait((completed, record))
                )
                next_index = index + 1
        except BaseException as error:
            arrival["producer_error"] = repr(error)
            stop.set()
        finally:
            for record in records[next_index:]:
                if "offered_monotonic" in record:
                    record["admission"] = "producer_failed"
                record["error"] = arrival.get(
                    "producer_error", "Producer stopped before this scheduled offer")
                completions.put_nowait((None, record))
            arrival["producer_finished_monotonic"] = time.perf_counter()

    def results():
        for _ in task_ids:
            future, record = completions.get()
            try:
                yield future, record
            finally:
                # The caller has finished native append/failure and timing retention.
                record["terminal_monotonic"] = time.perf_counter()
                if "reserved_monotonic" in record:
                    slots.release()

    producer = threading.Thread(target=produce, name="scale-arrival-producer", daemon=False)
    iterator = results()
    producer.start()
    try:
        yield iterator
    finally:
        stop.set()
        iterator.close()
        producer.join(PROCESS_CLEANUP_ALLOWANCE_SECONDS)
        arrival["producer_stopped"] = not producer.is_alive()
        if producer.is_alive():
            raise ValueError("Arrival producer stop is unknown")
        for future in futures:
            future.cancel()  # Running commands remain owned by the enclosing executor/supervisor.


def _arrival_receipt(arrival, raw_timings):
    """Reconstruct finite backlog from raw events, never from a timing percentile."""
    records = arrival["records"]
    epoch = arrival["epoch_monotonic"]
    rows = {row["task_id"]: row for row in raw_timings}
    if (not arrival["producer_stopped"] or len(rows) != len(records)
            or len(raw_timings) != len(records)):
        raise ValueError("Incomplete arrival accounting or unknown producer custody")
    # Equal timestamps follow this causal order, then task ID for stable replay.
    event_order = ("offered", "reserved", "started", "proof_ready", "ledger", "terminal")
    events = []
    for record in records:
        row = rows[record["task_id"]]
        for name, timestamp in (
            ("offered", record.get("offered_monotonic")),
            ("reserved", record.get("reserved_monotonic")),
            ("started", row.get("started_monotonic")),
            ("proof_ready", row.get("proof_ready_monotonic")),
            ("ledger", row.get("append_returned_monotonic")),
            ("terminal", record["terminal_monotonic"]),
        ):
            if timestamp is not None:
                events.append((timestamp, event_order.index(name), record["task_id"], name))
    events.sort()
    terminal = max(record["terminal_monotonic"] for record in records)
    duration = max(terminal, arrival["producer_finished_monotonic"]) - epoch
    # The public supervisor's maximum deadline plus cleanup bounds this series.
    if not 0 <= duration <= 3600 + PROCESS_CLEANUP_ALLOWANCE_SECONDS:
        raise ValueError("Arrival receipt exceeds the supervised time bound")
    by_task = {record["task_id"]: record for record in records}
    counts = Counter()
    in_flight, waiting, proof_ready = set(), set(), set()
    peaks = {"in_flight": 0, "waiting_to_start": 0, "proof_ready": 0}
    series = []
    index = 0
    for second in range(math.ceil(duration) + 1):
        while index < len(events) and events[index][0] <= epoch + second:
            _, _, task_id, event = events[index]
            counts[event] += 1
            if event == "reserved":
                in_flight.add(task_id)
                waiting.add(task_id)
            elif event == "started":
                waiting.discard(task_id)
            elif event == "proof_ready":
                proof_ready.add(task_id)
            elif event == "ledger":
                proof_ready.discard(task_id)
            elif event == "terminal":
                in_flight.discard(task_id)
                waiting.discard(task_id)
                proof_ready.discard(task_id)
                counts["failed"] += by_task[task_id]["terminal_outcome"] != "submitted"
            for name, values in (("in_flight", in_flight), ("waiting_to_start", waiting),
                                 ("proof_ready", proof_ready)):
                peaks[name] = max(peaks[name], len(values))
            index += 1
        series.append({"second": second, **{name: counts[name] for name in
                       ("offered", "started", "proof_ready", "ledger", "terminal", "failed")},
                       "in_flight": len(in_flight), "waiting_to_start": len(waiting),
                       "proof_ready_backlog": len(proof_ready)})
    offers = [record["offered_monotonic"] for record in records if "offered_monotonic" in record]
    gaps = [later - earlier for earlier, later in zip(offers, offers[1:], strict=False)]
    admissions = Counter(record["admission"] for record in records)
    return {
        **arrival,
        "event_tie_order": list(event_order),
        "scheduled": len(records), "actual_offered": len(offers),
        **{name: admissions[name] for name in
           ("admitted", "capacity_refused", "submission_failed", "producer_failed", "not_offered")},
        "handled": counts["terminal"], "failed": counts["failed"],
        "lateness_seconds": [record["offered_monotonic"] - record["scheduled_offer_monotonic"]
                             for record in records if "offered_monotonic" in record],
        "inter_offer_seconds": gaps,
        "observed_offers_per_second": ((len(offers) - 1) / (offers[-1] - offers[0])
                                       if len(offers) > 1 and offers[-1] > offers[0] else None),
        "peak_backlog": peaks, "one_second_series": series,
        "final_backlog": {"in_flight": len(in_flight), "waiting_to_start": len(waiting),
                          "proof_ready": len(proof_ready)},
        "final_terminal_monotonic": terminal,
        "drain_seconds": max(0, terminal - arrival["producer_finished_monotonic"]),
        "limits": [
            "The accepted 15ms p95 objective concerns ledger operations; historical fixture "
            "gates remain unchanged. proof_validation includes full preparation and retention.",
            "Arrival, admission, preparation wait, queue, completion and drain have no numeric "
            "workflow SLO. This finite host condition does not replace prior failed observations.",
        ],
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


# Observe the existing helper's sole Popen without another process or rewriting
# its cleanup code. The command cannot inherit this close-on-exec status pipe.
# A notification failure must NOT escape Popen/wait and bypass helper cleanup.
_COMMAND_STATUS_PRELUDE = r"""
import os
import subprocess

class ObservedCommand(subprocess.Popen):
    def notify(self, message):
        try:
            os.write(int(os.environ["ANVIL_SUBREAPER_STATUS_FD"]), message)
        except OSError:
            pass

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exit_notified = False
        self.notify(f"started:{self.pid}\n".encode("ascii"))

    def wait(self, *args, **kwargs):
        returncode = super().wait(*args, **kwargs)
        if not self.exit_notified:
            self.notify(f"exited:{returncode}\n".encode("ascii"))
            self.exit_notified = True
        return returncode

subprocess.Popen = ObservedCommand
"""
MAX_COMMAND_STATUS_BYTES = 128


def _command_status(status):
    match = re.fullmatch(
        rb"(?:started:([1-9][0-9]{0,19})\n)?(?:exited:(-?[0-9]{1,10})\n)?(1)?", status
    )
    if len(status) > MAX_COMMAND_STATUS_BYTES or match is None:
        raise ValueError("Invalid fixture command containment status")
    command_pid, exit_code, completed = match.groups()
    if (exit_code is not None and command_pid is None
            or completed is not None and exit_code is None):
        raise ValueError("Incomplete fixture command containment status")
    if exit_code is not None and not -64 <= int(exit_code) <= 255:
        raise ValueError("Invalid fixture command exit status")
    return (int(command_pid) if command_pid else None,
            int(exit_code) if exit_code else None, completed is not None)


def _require_linux_containment():
    if sys.platform != "linux" or not Path("/proc/self/stat").is_file():
        raise ValueError("Fixture command containment requires Linux subreaper and /proc support")


def _command(project, task_id, offered_at, command_timeout, command_argv):
    _require_linux_containment()
    started = datetime.now(UTC)
    began = time.perf_counter()
    environment = {"PATH": os.environ.get("PATH", os.defpath), "PYTHONIOENCODING": "utf-8"}
    # Preserve native executor configuration paths, never credential files.
    environment.update(
        {key: os.environ[key] for key in ("HOME", "XDG_CONFIG_HOME") if key in os.environ}
    )
    output = bytearray()
    status = bytearray()
    error = None
    forced_stop = False
    status_reader, status_writer = os.pipe()
    os.set_blocking(status_reader, False)
    environment["ANVIL_SUBREAPER_STATUS_FD"] = str(status_writer)
    process = None
    cleanup_requested = False
    try:
        # One extra supervisor per command, in the outer owned group/session.
        # Its subreaper also owns descendants that detach into another session.
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", _COMMAND_STATUS_PRELUDE + _LINUX_SUBREAPER,
             *command_argv], cwd=project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=environment, pass_fds=(status_writer,),
        )
        os.close(status_writer)
        status_writer = None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "output")
            selector.register(status_reader, selectors.EVENT_READ, "status")
            output_closed = False
            deadline = began + command_timeout
            while selector.get_map():
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError("Fixture command exceeded timeout or cleanup allowance")
                for key, _ in selector.select(min(remaining, 0.1)):
                    target = output if key.data == "output" else status
                    limit = (MAX_COMMAND_OUTPUT_BYTES if key.data == "output"
                             else MAX_COMMAND_STATUS_BYTES)
                    chunk = os.read(key.fd, min(65536, limit + 1 - len(target)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if key.data == "output":
                            output_closed = True
                        elif not _command_status(status)[2]:
                            raise ValueError("Missing fixture command completion handshake")
                        continue
                    target.extend(chunk)
                    if len(target) > limit:
                        del target[limit:]
                        raise ValueError(
                            f"Fixture command exceeded {key.data} limit; retained prefix")
                if output_closed and status.endswith(b"\n1") and not cleanup_requested:
                    _command_status(status)  # Strictly validate before requesting cleanup.
                    process.send_signal(signal.SIGUSR1)
                    cleanup_requested = True
                    deadline = time.perf_counter() + PROCESS_CLEANUP_ALLOWANCE_SECONDS
            process.wait(timeout=max(.001, deadline - time.perf_counter()))
    except Exception as caught:
        error = repr(caught)
    finally:
        if status_writer is not None:
            os.close(status_writer)
        if process is not None:
            if process.poll() is None:
                # The helper handles TERM by killing/reaping all owned descendants.
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    process.wait(timeout=PROCESS_CLEANUP_ALLOWANCE_SECONDS)
                except subprocess.TimeoutExpired:
                    forced_stop = True
                    process.kill()
                    process.wait(timeout=5)
            # Capture any final ownership notifications after the supervisor join.
            while len(status) <= MAX_COMMAND_STATUS_BYTES:
                try:
                    chunk = os.read(status_reader, MAX_COMMAND_STATUS_BYTES + 1 - len(status))
                except BlockingIOError:
                    break
                if not chunk:
                    break
                status.extend(chunk)
            process.stdout.close()
        os.close(status_reader)
    command_pid = exit_code = None
    completed = False
    try:
        command_pid, exit_code, completed = _command_status(status)
    except ValueError as caught:
        error = error or repr(caught)
    supervisor_exit = process.returncode if process is not None else None
    normalized_exit = exit_code if exit_code is None or exit_code >= 0 else 128 - exit_code
    containment_complete = (not forced_stop and command_pid is not None
                            and supervisor_exit != 126
                            and (supervisor_exit == 143 or
                                 completed and supervisor_exit == normalized_exit))
    if not (completed and cleanup_requested and containment_complete
            and supervisor_exit == normalized_exit):
        error = error or "Fixture command containment did not complete"
    ended_monotonic = time.perf_counter()
    ended = datetime.now(UTC)
    return {
        "task_id": task_id,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "ended_at": ended.isoformat().replace("+00:00", "Z"),
        "exit_code": exit_code,
        "output": bytes(output),
        "command_argv": command_argv,
        "queue_seconds": began - offered_at,
        "command_seconds": ended_monotonic - began,
        "offered_monotonic": offered_at,
        "started_monotonic": began,
        "ended_monotonic": ended_monotonic,
        "capture_protocol": "linux-subreaper-status.v1",
        "command_pid": command_pid,
        "command_reaped": completed or containment_complete,
        "command_supervisor_pid": process.pid if process is not None else None,
        "command_supervisor_exit_code": supervisor_exit,
        "command_supervisor_reaped": supervisor_exit is not None,
        "command_containment_complete": containment_complete,
        "error": error,
    }


class _ArtifactBudget:
    """Reserve flat capture writes; only the coordinator scans between native writes."""

    def __init__(self, work):
        self.work = work
        self.lock = threading.Lock()
        self.capture_bytes = 0
        self.reserved_names = set()
        self.capture_write_failed = False
        self.exhausted = False
        self.other_bytes = 0
        self.refresh()

    def refresh(self):
        with self.lock:
            if self.capture_write_failed:
                raise ValueError("Artifact accounting refused: capture write failed")
        # Workers can reserve/write captures throughout this scan. They never
        # write noncapture storage; the sole coordinator refreshes between its
        # native writes. Inventory captures without recursing or stat'ing each
        # already charged file, and publish against the latest reservations.
        other_bytes = 0
        capture_names = set()
        captures = self.work / "captures"
        try:
            if self.work.is_symlink() or captures.is_symlink() or not captures.is_dir():
                raise ValueError("Artifact accounting requires an owned captures directory")
            directories = [self.work]
            while directories:
                with os.scandir(directories.pop()) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            raise ValueError("Artifact accounting refuses symbolic links")
                        if Path(entry.path) == captures:
                            with os.scandir(captures) as retained:
                                for capture in retained:
                                    if not capture.is_file(follow_symlinks=False):
                                        raise ValueError(
                                            "Capture artifacts must be flat regular files")
                                    capture_names.add(capture.name)
                        elif entry.is_dir(follow_symlinks=False):
                            directories.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            other_bytes += entry.stat(follow_symlinks=False).st_size
                        else:
                            raise ValueError("Artifact accounting requires regular files")
        except (OSError, ValueError):
            with self.lock:
                self.exhausted = True
            raise
        with self.lock:
            # A writer may have failed while the inventory was being scanned.
            if self.capture_write_failed:
                raise ValueError("Artifact accounting refused: capture write failed")
            if capture_names - self.reserved_names:
                self.exhausted = True
                raise ValueError("Unaccounted capture artifact")
            self.other_bytes = other_bytes
            total = self.other_bytes + self.capture_bytes
            if total > MAX_STORAGE_BYTES:
                self.exhausted = True
                raise ValueError("Retained fixture exceeded 256 MiB storage budget")
            return total

    def write(self, path, raw):
        captures = self.work / "captures"
        if (path.parent != captures or self.work.is_symlink()
                or captures.is_symlink() or not captures.is_dir()):
            raise ValueError("Artifact writes require a flat owned capture path")
        with self.lock:
            if (self.exhausted
                    or self.other_bytes + self.capture_bytes + len(raw) > MAX_STORAGE_BYTES):
                self.exhausted = True
                raise ValueError("Retained fixture exceeded 256 MiB storage budget")
            # Charge before opening; a failed/partial write never returns capacity.
            self.capture_bytes += len(raw)
            self.reserved_names.add(path.name)
        try:
            with path.open("xb") as stream:
                if stream.write(raw) != len(raw):
                    raise OSError("Incomplete capture artifact write")
        except BaseException:
            # A failed exclusive open may have collided with unaccounted bytes.
            # Keep the charge, but never certify ambiguous ownership as clean.
            with self.lock:
                self.capture_write_failed = self.exhausted = True
            raise


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
        checkout / "benchmarks/harness/engine.py",
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
    arrival_rate=None,
):
    if not (
        1 <= tasks <= 2000
        and 1 <= workers <= min(tasks, 128)
        and 1 <= cohort_size <= 12
        and 1 <= command_timeout <= 30
        and durability in {"relaxed", "strict"}
        and events_storage in {"local", "git"}
        and proof_preparation in {"coordinator", "worker"}
        and (arrival_rate is None or type(arrival_rate) is int and 1 <= arrival_rate <= 1000)
    ):
        raise ValueError(
            "tasks=1..2000, workers=1..min(tasks,128), cohort=1..12, timeout=1..30; "
            "durability=relaxed|strict, events_storage=local|git, "
            "proof_preparation=coordinator|worker, arrival_rate=1..1000 or omitted"
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
    arrival_rate=None,
):
    """Run only below a fresh directory; the CLI additionally enforces a wall deadline."""
    _check_options(tasks=tasks, workers=workers, cohort_size=cohort_size,
                   command_timeout=command_timeout, durability=durability,
                   events_storage=events_storage, proof_preparation=proof_preparation,
                   arrival_rate=arrival_rate)
    if arrival_rate is not None and prepare_executor is not None:
        raise ValueError("arrival_rate is supported only by the native host executor")
    _require_linux_containment()
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
    arrival = None
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
            def submit(task_id, actual_offer):
                return executor.submit(
                    _execute_task, project, task_id, actual_offer, command_timeout,
                    command_for_task(task_id), task_snapshots[task_id], claim_snapshots[task_id],
                    budget, proof_preparation,
                )

            if arrival_rate is None:
                futures = {submit(task_id, offered_at): task_id for task_id in task_ids}
                results = contextlib.nullcontext(
                    (future, {"task_id": futures[future], "offered_monotonic": offered_at})
                    for future in as_completed(futures)
                )
            else:
                arrival = {"schema": "anvil.execution-arrivals.v1", "configured_rate": arrival_rate,
                           "epoch_monotonic": offered_at, "in_flight_limit": workers}
                results = _paced_results(
                    task_ids, submit, workers, arrival_rate, offered_at, arrival)
            with results as completed_results:
                for future, arrival_record in completed_results:
                    task_id = arrival_record["task_id"]
                    row = {"task_id": task_id, "claim_id": claims[task_id].id,
                           "claim_generation": claims[task_id].generation,
                           "task_revision": claims[task_id].attestation_context.task_revision,
                           "proof_preparation": proof_preparation,
                           **arrival_record, "outcome": "failed"}
                    execution = None
                    try:
                        if future is None:
                            raise ValueError(arrival_record.get(
                                "error", arrival_record["admission"]))
                        execution = future.result()
                        execution["handled_monotonic"] = time.perf_counter()
                        intervals.extend(
                            [(execution["started_monotonic"], 1),
                             (execution["ended_monotonic"], -1)]
                        )
                        for stage in ("queue", "command"):
                            measurements[stage].append(execution[f"{stage}_seconds"])
                        measurements["proof_queue"].append(
                            execution["handled_monotonic"] - execution["ended_monotonic"]
                        )
                        if proof_preparation == "coordinator" and execution["error"] is None:
                            _prepare_proof(project, task_snapshots[task_id],
                                           claim_snapshots[task_id], execution, budget)
                        if execution["error"] is not None:
                            raise ValueError(execution["error"])
                        if budget.exhausted:
                            raise ValueError("Retained fixture storage budget exhausted")
                        task = Task.model_validate_json(task_snapshots[task_id])
                        execution["writer_started_monotonic"] = time.perf_counter()
                        timed(
                            "evidence_append", _append, backend, "evidence.submitted",
                            "task", task_id,
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
                                            "command_pid", "command_reaped", "capture_protocol",
                                            "command_supervisor_pid",
                                            "command_supervisor_exit_code",
                                            "command_supervisor_reaped",
                                            "command_containment_complete")})
                            # Futures may remain until pool exit; discard duplicate artifact bytes.
                            execution.pop("output", None)
                            execution.pop("proof", None)
                        raw_timings.append(row)
                        try:
                            budget.write(captures / f"{task_id}.timing.json",
                                         json.dumps(row, sort_keys=True).encode())
                        except Exception as error:
                            failures.append({"task_id": task_id, "stage": "timing_retention",
                                             "error": repr(error)})
                            arrival_record["retention_error"] = repr(error)
                        arrival_record["terminal_outcome"] = (
                            "retention_failed" if "retention_error" in arrival_record
                            else row["outcome"])
                    if len(raw_timings) % 16 == 0:
                        try:
                            budget.refresh()
                        except ValueError as error:
                            failures.append({"stage": "storage", "error": repr(error)})
        execution_seconds = time.perf_counter() - offered_at
        if arrival is not None:
            arrival = _arrival_receipt(arrival, raw_timings)
            if arrival.get("producer_error"):
                failures.append({"stage": "arrival_producer", "error": arrival["producer_error"]})
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
        **({"arrival": arrival} if arrival is not None else {}),
        "source_sha256": source_identity,
        "command_containment": {
            "protocol": "linux-subreaper-status.v1",
            "supervisors_per_command": 1,
            "prelude_sha256": hashlib.sha256(_COMMAND_STATUS_PRELUDE.encode()).hexdigest(),
            "cleanup_allowance_seconds": PROCESS_CLEANUP_ALLOWANCE_SECONDS,
        },
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
        "command_captures": sum(row.get("command_pid") is not None for row in raw_timings),
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
            "Linux-only command containment adds one supervisor per command. Command latency "
            "includes supervisor startup, command execution, a 20ms fixture wait and cleanup; "
            "queue delay is separate. Cleanup allowance is a best-effort bound, not a hard OS cap.",
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
        cleanup_deadline = time.monotonic() + PROCESS_CLEANUP_ALLOWANCE_SECONDS
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        process.join(5)
        # A leader can exit before its subreapers finish adopted/detached cleanup.
        while time.monotonic() < cleanup_deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.01)
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
        _require_linux_containment()
        # A signal-zero probe changes no process. Refuse before spawning if
        # the environment denies the group control required for bounded cleanup.
        os.killpg(os.getpgrp(), 0)
    except (AttributeError, OSError, ValueError) as error:
        receipt = {
            "validated": False,
            "error": {
                "code": "unsupported_environment",
                "message": "Linux subreaper, /proc and process-group signaling are required "
                           "for cleanup.",
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
    parser.add_argument("--arrival-rate", type=int, help="Host-only offers per second (1..1000)")
    parser.add_argument("--deadline", type=int, default=900)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        _require_linux_containment()
    except ValueError as error:
        parser.error(str(error))
    if not 10 <= args.deadline <= 3600:
        parser.error("deadline must be 10..3600 seconds")
    output = args.output.resolve()
    if output.exists() or output.with_suffix(".work").exists():
        parser.error("output and work directory must be new; prior evidence is preserved")
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
    if args.arrival_rate is not None:
        options["arrival_rate"] = args.arrival_rate
        try:
            _check_options(**options)
        except ValueError as error:
            parser.error(str(error))
    output.parent.mkdir(parents=True, exist_ok=True)
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
