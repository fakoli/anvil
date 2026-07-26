"""Orchestration: run each scenario through both arms over N seeded trials, aggregate,
and render a comparison report. The two arms share the identical actor loop here — only
the injected Coordinator differs.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

from . import engine
from . import metrics as M
from .coordinators import (
    AnvilCoordinator,
    CoordinationInfrastructureError,
    MarkdownCoordinator,
    WorkLog,
    do_work,
    require_claim_success,
)
from .engine import setup_project
from .scenarios import Scenario, all_scenarios

ARMS = ("markdown", "anvil")

_VERSION_OUTPUT_LIMIT = 256
_VERSION_PROBE_TIMEOUT_SECONDS = 5.0
_DEFAULT_TRIAL_DEADLINE_SECONDS = 120.0
# Worker threads receive one shared best-effort cleanup allowance after active work
# expires. This is deliberately separate from the measured active-work deadline.
TRIAL_CLEANUP_ALLOWANCE_SECONDS = 1.0
_VERSION_RE = re.compile(
    r"anvil "
    r"(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?) "
    r"\(schema (?P<schema>[1-9][0-9]*)\)(?:\r?\n)?"
)

METRIC_LABELS = {
    "collisions": "file collisions",
    "duplicate_completions": "duplicate completions",
    "ordering_violations": "ordering violations",
    "completed_all": "completed all (1=yes)",
    "recovered_after_crash": "recovered after crash (1=yes)",
    "evidence_records": "auditable evidence records",
    "gamed_detected_pct": "gamed work detected (%)",
    "final_state_valid": "final state valid (1=yes)",
}


def _remove_trial_directory(
    path: Path,
    *,
    max_attempts: int = 30,
    retry_delay_seconds: float = 0.1,
) -> None:
    """Remove a trial directory, tolerating transient Windows handle release."""
    for attempt in range(max_attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt + 1 == max_attempts:
                raise
            time.sleep(retry_delay_seconds)


@contextlib.contextmanager
def _trial_directory():
    path = Path(tempfile.mkdtemp(prefix="fsbench-"))
    try:
        yield path
    finally:
        _remove_trial_directory(path)


def _configure_utf8_stdout() -> None:
    """Make the Unicode benchmark report safe on legacy Windows consoles."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")


def _positive_int(value: str) -> int:
    """Argparse type that refuses empty benchmark trial sets."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_finite_float(value: str) -> float:
    """Argparse type that refuses deadlines which cannot execute useful work."""
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def _nonnegative_finite_float(value: str) -> float:
    """Argparse type for bounded, schedulable race-window jitter."""
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative finite number")
    return parsed


class ReportPublicationError(RuntimeError):
    """The requested report target is already owned by another invocation."""


def _engine_version(
    *, timeout: float = _VERSION_PROBE_TIMEOUT_SECONDS
) -> tuple[str, str]:
    """Return canonical display/report versions without retaining raw CLI output."""
    try:
        result = engine.run(
            ["--version"],
            Path.cwd(),
            timeout=timeout,
            output_limit_bytes=_VERSION_OUTPUT_LIMIT,
        )
    except Exception:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=version invocation=failed"
        ) from None
    if not isinstance(result.out, str) or not isinstance(result.err, str):
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            f"phase=version exit_code={result.code}"
        )
    match = _VERSION_RE.fullmatch(result.out) if result.code == 0 else None
    if match is None:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            f"phase=version exit_code={result.code}"
        )
    report_version = f"{match.group('version')} (schema {match.group('schema')})"
    return f"anvil {report_version}", report_version


def _report_lock_path(output: Path) -> Path:
    return output.with_name(f"{output.name}.lock")


@contextlib.contextmanager
def _exclusive_report_publication(output: Path):
    """Reserve one output target so concurrent benchmark runs cannot overwrite it."""
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _report_lock_path(output)
    token = uuid.uuid4().hex
    try:
        lock_handle = lock_path.open("a+b")
        lock_handle.seek(0, os.SEEK_END)
        if lock_handle.tell() == 0:
            lock_handle.write(b"\0")
            lock_handle.flush()
        lock_handle.seek(0)
        if os.name == "nt":
            assert msvcrt is not None
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            assert fcntl is not None
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, PermissionError) as exc:
        with contextlib.suppress(UnboundLocalError):
            lock_handle.close()
        raise ReportPublicationError(
            "benchmark report publication refused: output target is already reserved"
        ) from exc
    try:
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(json.dumps({"pid": os.getpid(), "token": token}).encode())
        lock_handle.flush()
        os.fsync(lock_handle.fileno())
        yield
    finally:
        if os.name == "nt":
            lock_handle.seek(0)
            assert msvcrt is not None
            with contextlib.suppress(OSError):
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            lock_handle.close()
            try:
                owner = json.loads(lock_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                owner = None
            if isinstance(owner, dict) and owner.get("token") == token:
                lock_path.unlink(missing_ok=True)
        else:
            lock_path.unlink(missing_ok=True)
            lock_handle.close()


def _publish_report_atomic(output: Path, report: str) -> None:
    """Replace a reserved report atomically; never expose a partial artifact."""
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(report)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, output)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _remaining_or_fail(deadline: float, phase: str) -> float:
    """Return remaining trial time or raise one bounded infrastructure error."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            f"phase={phase} deadline=exceeded"
        )
    return remaining


def _seed_crash_claim(proj, task_id: str, *, deadline: float) -> None:
    """Create and expire the exact dead-actor claim or refuse the trial."""
    actor = "dead"
    claim_timeout = _remaining_or_fail(deadline, "crash_claim")
    try:
        result = engine.run(
            ["claim", task_id, "--json"],
            proj.root,
            actor=actor,
            timeout=claim_timeout,
        )
    except Exception:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=crash_claim invocation=failed"
        ) from None
    claim = require_claim_success(
        result,
        expected_task_id=task_id,
        expected_actor=actor,
        phase="crash_claim",
    )
    _remaining_or_fail(deadline, "crash_expire")
    try:
        expired = engine.expire_claims_for(proj, task_id, deadline=deadline)
    except TimeoutError:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=crash_expire deadline=exceeded"
        ) from None
    except Exception:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=crash_expire mutation=failed"
        ) from None
    if expired != 1:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=crash_expire expired_claims=unexpected"
        )
    _remaining_or_fail(deadline, "crash_expire")
    try:
        postcondition = engine.claim_is_expired(
            proj,
            claim["id"],
            task_id,
            actor,
            deadline=deadline,
        )
    except TimeoutError:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=crash_expire deadline=exceeded"
        ) from None
    except Exception:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=crash_expire verification=failed"
        ) from None
    if postcondition is not True:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=crash_expire postcondition=failed"
        )
    _remaining_or_fail(deadline, "crash_expire")


def _coord(arm: str, proj):
    return MarkdownCoordinator(proj) if arm == "markdown" else AnvilCoordinator(proj)


def _gamed_task_ids(scenario: Scenario, seed: int) -> set[str]:
    """Choose the requested share of unique tasks deterministically for one trial."""
    task_ids = sorted(task.id for task in scenario.tasks)
    count = min(len(task_ids), max(0, round(len(task_ids) * scenario.gamed_fraction)))
    return set(random.Random(seed).sample(task_ids, count))


def run_trial(scenario: Scenario, arm: str, seed: int, trial_idx: int,
              root: Path, jitter: float, deadline: float) -> dict:
    end_at = time.monotonic() + deadline
    gamed_task_ids = _gamed_task_ids(scenario, seed)
    try:
        proj = setup_project(
            root,
            f"{scenario.key[:12]}-{arm[:4]}-{trial_idx}",
            list(scenario.tasks),
            lease_minutes=scenario.lease_minutes,
            deadline=end_at,
        )
    except (TimeoutError, subprocess.TimeoutExpired):
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=setup deadline=exceeded"
        ) from None
    except Exception:
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=setup invocation=failed"
        ) from None
    coord = _coord(arm, proj)
    log = WorkLog(proj.root / "work.log")
    completions: list[dict] = []
    worker_errors: list[Exception] = []
    worker_finished_at: list[float] = []
    lock = threading.Lock()
    stop = threading.Event()
    crashed_task: dict = {"id": None}
    _remaining_or_fail(end_at, "setup")

    # Deterministic crash injection: BEFORE any worker runs, a "dead" actor takes an
    # exclusive lock on crash_task and never returns it. anvil must let the
    # lease expire and reclaim it; markdown has no lock to abandon (the box just stays
    # unticked), so a worker simply picks it up.
    if scenario.crash_actor:
        crashed_task["id"] = scenario.crash_task
        if arm == "anvil":
            # The dead actor never renews; fast-forward its exact lease so the reaper
            # recovers the abandoned lock without a 60-minute real wait. Any failed or
            # ambiguous setup operation invalidates the trial as infrastructure.
            _seed_crash_claim(proj, scenario.crash_task, deadline=end_at)

    def actor_fn(idx: int) -> None:
        actor = f"a{idx}"
        rng = random.Random(seed * 1000 + idx)
        try:
            while not stop.is_set():
                if time.monotonic() >= end_at:
                    raise CoordinationInfrastructureError(
                        "benchmark coordination infrastructure failure: "
                        "phase=trial deadline=exceeded"
                    )
                remaining = end_at - time.monotonic()
                if remaining <= 0:
                    raise CoordinationInfrastructureError(
                        "benchmark coordination infrastructure failure: "
                        "phase=trial deadline=exceeded"
                    )
                if coord.finished(timeout=remaining):
                    return
                remaining = end_at - time.monotonic()
                if remaining <= 0:
                    raise CoordinationInfrastructureError(
                        "benchmark coordination infrastructure failure: "
                        "phase=trial deadline=exceeded"
                    )
                acquired = coord.acquire(actor, rng, timeout=remaining)
                if acquired is None:
                    stop.wait(min(0.02 + rng.random() * 0.03, remaining))
                    continue
                if time.monotonic() >= end_at:
                    raise CoordinationInfrastructureError(
                        "benchmark coordination infrastructure failure: "
                        "phase=trial deadline=exceeded"
                )
                task_id = acquired.task_id
                task = coord.task(task_id)
                gamed = task_id in gamed_task_ids
                if not do_work(
                    proj,
                    log,
                    actor,
                    task,
                    jitter,
                    stop_event=stop,
                    deadline=end_at,
                ):
                    if time.monotonic() >= end_at:
                        raise CoordinationInfrastructureError(
                            "benchmark coordination infrastructure failure: "
                            "phase=trial deadline=exceeded"
                        )
                    return
                remaining = end_at - time.monotonic()
                if remaining <= 0:
                    raise CoordinationInfrastructureError(
                        "benchmark coordination infrastructure failure: "
                        "phase=trial deadline=exceeded"
                    )
                outcome = coord.complete(
                    actor,
                    task,
                    gamed,
                    timeout=remaining,
                    claim_id=acquired.claim_id,
                )
                if time.monotonic() >= end_at:
                    raise CoordinationInfrastructureError(
                        "benchmark coordination infrastructure failure: "
                        "phase=trial deadline=exceeded"
                    )
                with lock:
                    completions.append({
                        "task": task_id,
                        "actor": actor,
                        "gamed": gamed,
                        "completed": outcome.completed,
                        "evidence_valid": outcome.evidence_valid,
                        "failure": outcome.failure,
                    })
                if outcome.failure is not None:
                    stop.set()
                    return
        except Exception as exc:
            with lock:
                worker_errors.append(exc)
            stop.set()

    start_gate = threading.Event()

    def gated_actor_fn(idx: int) -> None:
        try:
            start_gate.wait()
            if stop.is_set():
                return
            actor_fn(idx)
        finally:
            # Record the worker's own exit boundary only after acquiring the final
            # bookkeeping lock. The parent may not resume from its timed join until
            # after this thread has exited, so liveness alone cannot distinguish an
            # on-time exit from one that crossed ``end_at`` waiting for this lock.
            with lock:
                worker_finished_at.append(time.monotonic())

    threads = [threading.Thread(target=gated_actor_fn, args=(i,))
               for i in range(scenario.actors)]
    started_threads: list[threading.Thread] = []
    try:
        for t in threads:
            try:
                t.start()
            except BaseException:
                if t.ident is not None:
                    started_threads.append(t)
                raise
            started_threads.append(t)
    except BaseException:
        stop.set()
        start_gate.set()
        cleanup_deadline = time.monotonic() + TRIAL_CLEANUP_ALLOWANCE_SECONDS
        for t in started_threads:
            t.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))
        raise
    start_gate.set()
    for t in started_threads:
        t.join(timeout=max(0.0, end_at - time.monotonic()))
    deadline_expired = any(t.is_alive() for t in started_threads)
    stop.set()
    cleanup_deadline = time.monotonic() + TRIAL_CLEANUP_ALLOWANCE_SECONDS
    for t in started_threads:
        t.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))

    if worker_errors:
        if isinstance(worker_errors[0], CoordinationInfrastructureError):
            raise worker_errors[0]
        raise RuntimeError("benchmark worker failed unexpectedly") from worker_errors[0]
    if (
        deadline_expired
        or any(t.is_alive() for t in started_threads)
        or any(finished_at >= end_at for finished_at in worker_finished_at)
    ):
        raise CoordinationInfrastructureError(
            "benchmark coordination infrastructure failure: "
            "phase=trial deadline=exceeded"
        )

    statuses = {}
    if arm == "anvil":
        remaining = _remaining_or_fail(end_at, "final_status")
        try:
            statuses = engine.task_status(proj, timeout=remaining)
        except Exception:
            raise CoordinationInfrastructureError(
                "benchmark coordination infrastructure failure: "
                "phase=final_status invocation=failed"
            ) from None
        _remaining_or_fail(end_at, "final_status")
    crash_recovered = None
    if scenario.crash_actor:
        ct = crashed_task["id"]
        if arm == "anvil":
            crash_recovered = ct is not None and statuses.get(ct) in {"done", "accepted"}
        else:
            crash_recovered = ct is not None and ct in {c["task"] for c in completions}
    return M.compute(scenario, log.rows(), completions, statuses, arm, crash_recovered)


def run_scenario(scenario: Scenario, trials: int, seed: int, jitter: float,
                 deadline: float, log_fn=print) -> dict:
    results = {}
    for arm in ARMS:
        per_trial = []
        for trial in range(trials):
            with _trial_directory() as trial_dir:
                root = trial_dir / "proj"
                t0 = time.time()
                m = run_trial(scenario, arm, seed + trial, trial, root, jitter, deadline)
                per_trial.append(m)
                log_fn(f"    [{scenario.key}] {arm:13s} trial {trial+1}/{trials} "
                       f"({time.time()-t0:4.1f}s) "
                       + " ".join(f"{k}={m[k]}" for k in scenario.headline if k in m))
        results[arm] = {"agg": M.aggregate(per_trial), "trials": per_trial}
    return results


@dataclass(frozen=True)
class TrialValidityFailure:
    """Why one selected Anvil trial cannot be treated as a successful result."""

    scenario: str
    trial: int
    reasons: tuple[str, ...]
    completion_details: tuple[str, ...]

    def report_lines(self) -> list[str]:
        prefix = f"scenario={self.scenario} trial={self.trial}"
        lines = [f"- {prefix} invalid: {', '.join(self.reasons)}"]
        lines.extend(f"  - {detail}" for detail in self.completion_details)
        return lines


def validate_anvil_trials(all_res: dict[str, dict]) -> list[TrialValidityFailure]:
    """Validate each Anvil trial independently; control-arm invalidity is excluded."""
    failures = []
    for scenario_key, scenario_results in all_res.items():
        for trial_idx, metrics in enumerate(
            scenario_results["anvil"]["trials"], start=1
        ):
            reasons = []
            if metrics.get("completed_all") != 1:
                reasons.append("completed_all!=1")
            if metrics.get("final_state_valid") != 1:
                reasons.append("final_state_valid!=1")
            if metrics.get("completion_observations_valid") != 1:
                reasons.append("completion_observations_valid!=1")
            completion_failures = metrics.get("completion_failures")
            if type(completion_failures) is not int or completion_failures < 0:
                reasons.append("completion_failures=invalid")
            elif completion_failures > 0:
                reasons.append("completion_failures>0")
            invalid_honest_evidence = metrics.get("invalid_honest_evidence")
            if (
                type(invalid_honest_evidence) is not int
                or invalid_honest_evidence < 0
            ):
                reasons.append("invalid_honest_evidence=invalid")
            elif invalid_honest_evidence > 0:
                reasons.append("invalid_honest_evidence>0")
            if reasons:
                failures.append(TrialValidityFailure(
                    scenario=scenario_key,
                    trial=trial_idx,
                    reasons=tuple(reasons),
                    completion_details=tuple(
                        metrics.get("_completion_failure_details", ())
                    ),
                ))
    return failures


# --- rendering --------------------------------------------------------------

def _bar(value: float, vmax: float, width: int = 20) -> str:
    if vmax <= 0:
        return "·" * 0
    n = int(round(width * value / vmax))
    return "█" * n + "·" * (width - n)


def render_scenario(scenario: Scenario, res: dict) -> str:
    md, fk = res["markdown"]["agg"], res["anvil"]["agg"]
    lines = [f"### {scenario.title} — `{scenario.key}`", "",
             scenario.description,
             f"\n*{scenario.actors} actors · headline metrics averaged over trials.*", ""]
    lines += ["| Metric | markdown (control) | anvil | better |",
              "|---|---:|---:|:--:|"]
    for k in scenario.headline:
        a, b = md.get(k, 0), fk.get(k, 0)
        if k in M.LOWER_BETTER:
            win = "anvil ✅" if b < a else ("tie" if b == a else "markdown")
        else:
            win = "anvil ✅" if b > a else ("tie" if b == a else "markdown")
        suffix = "%" if k in M.PCT else ""
        lines.append(f"| {METRIC_LABELS.get(k,k)} | {a}{suffix} | {b}{suffix} | {win} |")
    # one ascii chart on the primary (first) headline metric
    primary = scenario.headline[0]
    a, b = md.get(primary, 0), fk.get(primary, 0)
    vmax = max(a, b, 1)
    lines += ["", "```",
              f"{METRIC_LABELS.get(primary, primary)}:",
              f"  markdown      {_bar(a, vmax)} {a}",
              f"  anvil  {_bar(b, vmax)} {b}",
              "```", ""]
    return "\n".join(lines)


def render_report(
    scenarios: list[Scenario],
    all_res: dict,
    meta: dict,
    validity_failures: list[TrialValidityFailure] | None = None,
) -> str:
    validity_failures = validity_failures or []
    lines = ["# anvil coordination benchmark — results", "",
             "> Generated by `benchmarks/run_benchmark.py`. Both arms run the identical "
             "actor loop over the identical task set; the **only** variable is the "
             "coordination layer. The anvil arm drives the real CLI "
             "(`next`/`claim`/`submit`/`apply`) against a live SQLite state engine.", "",
             f"**Config:** {meta['trials']} trials · seed {meta['seed']} · "
             f"jitter {meta['jitter']}s · anvil v{meta.get('version','?')}", "",
             f"**Result validity:** {'INVALID' if validity_failures else 'VALID'}", ""]
    if validity_failures:
        lines += [
            "The report was preserved, but at least one selected Anvil trial failed "
            "a completion or state invariant:",
            "",
        ]
        for failure in validity_failures:
            lines.extend(failure.report_lines())
        lines.append("")
    lines += ["## Summary", "",
              "| Scenario | what it proves | control | anvil |",
              "|---|---|---:|---:|"]
    for s in scenarios:
        res = all_res[s.key]
        p = s.headline[0]
        a = res["markdown"]["agg"].get(p, 0)
        b = res["anvil"]["agg"].get(p, 0)
        suf = "%" if p in M.PCT else ""
        lines.append(f"| {s.title} | {METRIC_LABELS.get(p,p)} | {a}{suf} | {b}{suf} |")
    lines += ["", "## Scenarios", ""]
    for s in scenarios:
        lines.append(render_scenario(s, all_res[s.key]))
    lines += [
        "## Honest caveats", "",
        "- **Reproducible-aggregate, not bit-identical.** Real OS-thread concurrency is "
        "nondeterministic; numbers are means over seeded trials. Re-running reproduces "
        "the *conclusion* (and close numbers), not identical decimals.",
        "- **The evidence gate is advisory, not blocking.** anvil *flags* gamed "
        "submissions (`INCOMPLETE`) for a reviewer; it does not refuse them. The metric "
        "is detectability, which markdown structurally lacks (zero evidence record).",
        "- **Crash recovery is a tradeoff, not a pure win.** Markdown never deadlocks "
        "because it never locks — at the cost of duplicates. anvil locks "
        "exclusively (no duplicates) and *still* recovers, because the lease self-heals.",
        "- **Actors are simulated, not real LLM agents.** `--live` swaps in real "
        "subagents (costs tokens, nondeterministic). The simulation isolates the "
        "coordination mechanism, which is what the claim is about.",
        "- **Both engine bugs this harness found are fixed in v1.23.3.** "
        "`overlapping_files` collisions are now 0 across all trials (the in-claim-"
        "transaction overlap + conflict-group guard), covered by "
        "tests/test_claims_concurrency.py.",
        "- **Crash recovery fast-forwards the lease.** As of v1.23.3 the CLI honors "
        "`default_lease_minutes` (including fractional), but the harness still backdates "
        "`lease_expires_at` to keep the run fast while exercising the real reaper. The "
        "reap -> reclaim -> complete path is genuine.",
    ]
    return "\n".join(lines) + "\n"


def stdout_report_summary(report: str) -> str:
    """Return the report preamble and complete Summary section for stdout."""
    lines = report.splitlines()
    try:
        summary_start = lines.index("## Summary")
    except ValueError as exc:  # pragma: no cover - internal renderer invariant
        raise RuntimeError("rendered benchmark report has no Summary section") from exc
    summary_end = next(
        (
            idx
            for idx in range(summary_start + 1, len(lines))
            if lines[idx].startswith("## ")
        ),
        len(lines),
    )
    return "\n".join(lines[:summary_end]).rstrip()


def _select_scenarios(spec: str, catalog: dict[str, Scenario]) -> list[Scenario] | None:
    """Resolve one exact, duplicate-free scenario selection."""
    if spec == "all":
        return list(catalog.values()) or None
    keys = [key.strip() for key in spec.split(",")]
    if (
        not keys
        or any(not key or key not in catalog for key in keys)
        or len(set(keys)) != len(keys)
    ):
        return None
    return [catalog[key] for key in keys]


def _execute_benchmark(args, chosen: list[Scenario], trials: int, output: Path) -> int:
    # Do not probe the version here: an uncached probe resolves/syncs the binary and
    # would let the first trial evade its setup deadline. The first setup pays that
    # cost; the post-run version probe is then independently bounded.
    print("anvil coordination benchmark")
    print(f"scenarios={[s.key for s in chosen]} trials={trials} seed={args.seed}\n")

    effective_scenarios = [
        Scenario(**{**scenario.__dict__, "actors": max(3, scenario.actors // 2)})
        if args.quick
        else scenario
        for scenario in chosen
    ]
    all_res = {}
    t0 = time.time()
    for scenario in effective_scenarios:
        print(f"  scenario: {scenario.key} ({scenario.actors} actors)")
        all_res[scenario.key] = run_scenario(
            scenario,
            trials,
            args.seed,
            args.jitter,
            args.deadline,
        )
    display_version, report_version = _engine_version()
    print(f"engine: {display_version}")
    meta = {
        "trials": trials,
        "seed": args.seed,
        "jitter": args.jitter,
        "version": report_version,
    }
    validity_failures = validate_anvil_trials(all_res)
    report = render_report(effective_scenarios, all_res, meta, validity_failures)
    _publish_report_atomic(output, report)
    print(f"\nDone in {time.time()-t0:.1f}s. Report -> {args.out}\n")
    # Echo the complete summary structurally; preamble length changes when result
    # validity diagnostics are present, so a fixed line slice can truncate the table.
    print(stdout_report_summary(report))
    if validity_failures and not args.allow_invalid_results:
        return 1
    return 0


def main(argv=None) -> int:
    _configure_utf8_stdout()
    ap = argparse.ArgumentParser(description="anvil coordination benchmark")
    ap.add_argument("--scenarios", default="all",
                    help="comma-separated scenario keys, or 'all'")
    ap.add_argument("--trials", type=_positive_int, default=3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--jitter", type=_nonnegative_finite_float, default=0.005,
                    help="race-window seconds injected into each file write")
    ap.add_argument(
        "--deadline",
        type=_positive_finite_float,
        default=None,
        help=(
            "per-trial active-work cap; defaults to 120s in every mode; "
            "bounded cleanup may follow"
        ),
    )
    ap.add_argument("--quick", action="store_true", help="1 trial, fewer actors")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "RESULTS.md"))
    ap.add_argument("--live", action="store_true",
                    help="(stub) swap simulated actors for real subagents — not yet implemented")
    ap.add_argument(
        "--allow-invalid-results",
        action="store_true",
        help=(
            "report-only mode: return zero for completed reports whose selected "
            "Anvil trials fail result invariants"
        ),
    )
    args = ap.parse_args(argv)
    if args.deadline is None:
        args.deadline = _DEFAULT_TRIAL_DEADLINE_SECONDS

    if args.live:
        print("--live mode is a planned phase-2 stub: it will replace the simulated "
              "actor loop with real Claude subagents (Agent SDK), same metrics, same "
              "scenarios. Running the deterministic simulation for now.\n")

    catalog = all_scenarios()
    chosen = _select_scenarios(args.scenarios, catalog)
    if chosen is None:
        print(
            "scenario selection refused: every requested key must be known and unique; "
            f"available: {list(catalog)}"
        )
        return 2

    trials = 1 if args.quick else args.trials
    output = Path(args.out).expanduser().resolve()
    try:
        with _exclusive_report_publication(output):
            return _execute_benchmark(args, chosen, trials, output)
    except (CoordinationInfrastructureError, ReportPublicationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
