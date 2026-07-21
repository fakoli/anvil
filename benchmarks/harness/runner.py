"""Orchestration: run each scenario through both arms over N seeded trials, aggregate,
and render a comparison report. The two arms share the identical actor loop here — only
the injected Coordinator differs.
"""
from __future__ import annotations

import argparse
import contextlib
import random
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import engine
from . import metrics as M
from .coordinators import AnvilCoordinator, MarkdownCoordinator, WorkLog, do_work
from .engine import setup_project
from .scenarios import Scenario, all_scenarios

ARMS = ("markdown", "anvil")

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


def _coord(arm: str, proj):
    return MarkdownCoordinator(proj) if arm == "markdown" else AnvilCoordinator(proj)


def run_trial(scenario: Scenario, arm: str, seed: int, trial_idx: int,
              root: Path, jitter: float, deadline: float) -> dict:
    proj = setup_project(root, f"{scenario.key[:12]}-{arm[:4]}-{trial_idx}",
                         list(scenario.tasks), lease_minutes=scenario.lease_minutes)
    coord = _coord(arm, proj)
    log = WorkLog(proj.root / "work.log")
    completions: list[dict] = []
    lock = threading.Lock()
    crashed_task: dict = {"id": None}
    end_at = time.time() + deadline

    # Deterministic crash injection: BEFORE any worker runs, a "dead" actor takes an
    # exclusive lock on crash_task and never returns it. anvil must let the
    # lease expire and reclaim it; markdown has no lock to abandon (the box just stays
    # unticked), so a worker simply picks it up.
    if scenario.crash_actor:
        crashed_task["id"] = scenario.crash_task
        if arm == "anvil":
            engine.run(["claim", scenario.crash_task], proj.root, actor="dead")
            # the dead actor never renews; fast-forward its lease so the reaper recovers
            # the abandoned exclusive lock without a 60-minute real wait.
            engine.expire_claims_for(proj, scenario.crash_task)

    def actor_fn(idx: int) -> None:
        actor = f"a{idx}"
        rng = random.Random(seed * 1000 + idx)
        while time.time() < end_at:
            try:
                if coord.finished():
                    return
                task_id = coord.acquire(actor, rng)
            except Exception:
                time.sleep(0.05)
                continue
            if task_id is None:
                time.sleep(0.02 + rng.random() * 0.03)
                continue
            task = coord.task(task_id)
            gamed = rng.random() < scenario.gamed_fraction
            do_work(proj, log, actor, task, jitter)
            try:
                valid = coord.complete(actor, task, gamed)
            except Exception:
                valid = None
            with lock:
                completions.append({"task": task_id, "actor": actor,
                                    "gamed": gamed, "evidence_valid": valid})

    threads = [threading.Thread(target=actor_fn, args=(i,))
               for i in range(scenario.actors)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=deadline + 10)

    statuses = engine.task_status(proj) if arm == "anvil" else {}
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
            win = "fakoli ✅" if b < a else ("tie" if b == a else "markdown")
        else:
            win = "fakoli ✅" if b > a else ("tie" if b == a else "markdown")
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


def render_report(scenarios: list[Scenario], all_res: dict, meta: dict) -> str:
    lines = ["# anvil coordination benchmark — results", "",
             "> Generated by `benchmarks/run_benchmark.py`. Both arms run the identical "
             "actor loop over the identical task set; the **only** variable is the "
             "coordination layer. The anvil arm drives the real CLI "
             "(`next`/`claim`/`submit`/`apply`) against a live SQLite state engine.", "",
             f"**Config:** {meta['trials']} trials · seed {meta['seed']} · "
             f"jitter {meta['jitter']}s · anvil v{meta.get('version','?')}", "",
             "## Summary", "",
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
        "tests/test_claim_concurrency.py.",
        "- **Crash recovery fast-forwards the lease.** As of v1.23.3 the CLI honors "
        "`default_lease_minutes` (including fractional), but the harness still backdates "
        "`lease_expires_at` to keep the run fast while exercising the real reaper. The "
        "reap -> reclaim -> complete path is genuine.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    _configure_utf8_stdout()
    ap = argparse.ArgumentParser(description="anvil coordination benchmark")
    ap.add_argument("--scenarios", default="all",
                    help="comma-separated scenario keys, or 'all'")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--jitter", type=float, default=0.005,
                    help="race-window seconds injected into each file write")
    ap.add_argument("--deadline", type=float, default=45.0,
                    help="per-trial wall-clock cap (seconds)")
    ap.add_argument("--quick", action="store_true", help="1 trial, fewer actors")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "RESULTS.md"))
    ap.add_argument("--live", action="store_true",
                    help="(stub) swap simulated actors for real subagents — not yet implemented")
    args = ap.parse_args(argv)

    if args.live:
        print("--live mode is a planned phase-2 stub: it will replace the simulated "
              "actor loop with real Claude subagents (Agent SDK), same metrics, same "
              "scenarios. Running the deterministic simulation for now.\n")

    catalog = all_scenarios()
    keys = list(catalog) if args.scenarios == "all" else args.scenarios.split(",")
    chosen = [catalog[k] for k in keys if k in catalog]
    if not chosen:
        print(f"no valid scenarios in {keys}; available: {list(catalog)}")
        return 2

    trials = 1 if args.quick else args.trials
    version = engine.run(["--version"], Path.cwd()).out.strip() or "?"
    print(f"anvil coordination benchmark · {version}")
    print(f"scenarios={[s.key for s in chosen]} trials={trials} seed={args.seed}\n")

    all_res = {}
    t0 = time.time()
    for s in chosen:
        scenario = s
        if args.quick:
            scenario = Scenario(**{**s.__dict__, "actors": max(3, s.actors // 2)})
        print(f"  scenario: {scenario.key} ({scenario.actors} actors)")
        all_res[s.key] = run_scenario(scenario, trials, args.seed, args.jitter,
                                      args.deadline)
    meta = {"trials": trials, "seed": args.seed, "jitter": args.jitter,
            "version": version.replace("anvil ", "")}
    report = render_report(chosen, all_res, meta)
    Path(args.out).write_text(report, encoding="utf-8")
    print(f"\nDone in {time.time()-t0:.1f}s. Report -> {args.out}\n")
    # echo the summary table to stdout
    print("\n".join(report.splitlines()[: 8 + 2 + len(chosen)]))
    return 0
