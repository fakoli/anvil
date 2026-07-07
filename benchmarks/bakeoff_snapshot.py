"""B50 bake-off — one-shot metrics snapshot for the two-week measurement log.

The bake-off (docs/plans/bake-off.md) measures whether the capacity-coordination
premise holds before any capacity-pool machinery is built. This script captures
the LIVE, in-engine half of the daily measurement in one JSON line so it can be
logged over the two weeks and charted:

  - review debt: needs_review depth (the binding constraint) + task status counts
  - per-runner accept-rate (B49) — is local work being accepted or reworked?
  - packet right-sizing savings (B51) — token reduction the fast-lane delivers

The OUT-OF-ENGINE half (per-pool throttle frequency, spillover frequency,
cloud-tokens, review-minutes, and the SL-2 local-vs-Sonnet false-pass run via
benchmarks/critic_falsepass.py) is captured by the runbook, not here.

    python benchmarks/bakeoff_snapshot.py <state_dir> [--actor NAME]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def snapshot(state_dir: Path, *, actor: str | None = None) -> dict[str, Any]:
    """Capture the live, in-engine bake-off metrics for one project.

    Raises FileNotFoundError if ``state_dir/state.db`` is absent — fail fast on a
    wrong path rather than silently initializing an empty DB and logging an
    all-zero snapshot.
    """
    from anvil.claims.metrics import AcceptRateMetrics
    from anvil.clock import SystemClock
    from anvil.context.packet_metrics import measure_backlog
    from anvil.state.models import TaskStatus
    from anvil.state.sqlite import SqliteBackend

    db_file = state_dir / "state.db"
    if not db_file.exists():
        raise FileNotFoundError(
            f"no anvil state at {db_file} — pass an initialized project's "
            ".anvil directory (refusing to create an empty DB)."
        )
    clock = SystemClock()
    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(state_dir / "events.jsonl"),
        clock=clock,
    )
    backend.initialize()
    try:
        all_tasks = backend.list_tasks()
        status_counts: dict[str, int] = {}
        for task in all_tasks:
            status_counts[task.status.value] = status_counts.get(task.status.value, 0) + 1

        metrics = AcceptRateMetrics(backend, clock)
        # Per-runner accept-rate over every runner that has submitted evidence.
        actors = sorted({ev.submitted_by for ev in backend.list_evidence()})
        accept_rates = {a: metrics.accept_rate(a) for a in actors}

        packet = measure_backlog(all_tasks)
    finally:
        backend.close()

    return {
        "needs_review_depth": status_counts.get(TaskStatus.needs_review.value, 0),
        "task_status_counts": status_counts,
        "accept_rate_by_runner": accept_rates,
        "queried_actor": actor,
        "queried_actor_accept_rate": accept_rates.get(actor) if actor else None,
        "packet_sizing": {
            "total_tasks": packet["total_tasks"],
            "routed_lightweight": packet["routed_lightweight"],
            "as_routed_savings_pct": packet["as_routed_savings_pct"],
        },
    }


def _main(argv: list[str] | None = None) -> int:
    usage = "usage: python benchmarks/bakeoff_snapshot.py <state_dir> [--actor NAME]"
    args = list(argv if argv is not None else sys.argv[1:])
    actor: str | None = None
    if "--actor" in args:
        i = args.index("--actor")
        if i + 1 >= len(args):
            print(f"error: --actor requires a value\n{usage}", file=sys.stderr)
            return 2
        actor = args[i + 1]
        del args[i : i + 2]
    if not args:
        print(usage, file=sys.stderr)
        return 2
    try:
        snap = snapshot(Path(args[0]).expanduser(), actor=actor)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # One compact line so the output appends cleanly to a .jsonl bake-off log.
    print(json.dumps(snap, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via _main in tests
    raise SystemExit(_main())
