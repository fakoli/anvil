"""Packet-quality measurement (B51) — quantify the token reduction of
right-sized (lightweight) vs full work packets.

Packet quality is a first-class, *measured* workstream: a tight, right-sized
packet lets a fast local model skip exploration AND cuts tokens-to-completion,
which directly buys capacity (the binding constraint). This module measures the
token reduction deterministically across a backlog so the effect is tracked, not
asserted. The complementary *local-model success-rate lift* requires a live run
and is measured in the B50 bake-off — token reduction is the part we can quantify
here without a model.

Token counts use a whitespace-split proxy (no tokenizer dependency); the absolute
numbers are approximate but the *relative* reduction is stable, which is what a
right-sizing workstream tracks. Run standalone on a real project:

    python -m anvil.context.packet_metrics <state_dir>
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anvil.state.models import Task

__all__ = ["PacketSizing", "measure_task", "measure_backlog"]


def _token_estimate(text: str) -> int:
    """Whitespace-token proxy for an LLM token count (no tokenizer dependency)."""
    return len(text.split())


@dataclass(frozen=True)
class PacketSizing:
    """Per-task token measurement of the lightweight vs full packet."""

    task_id: str
    routed_variant: str  # what fast-lane would actually route this task to
    full_tokens: int
    lightweight_tokens: int

    @property
    def reduction_tokens(self) -> int:
        return self.full_tokens - self.lightweight_tokens

    @property
    def reduction_pct(self) -> float:
        if self.full_tokens == 0:
            return 0.0
        return round(100.0 * self.reduction_tokens / self.full_tokens, 1)


def measure_task(task: Task) -> PacketSizing:
    """Render *task* as both a full and a lightweight packet and measure both.

    Context (deps/decisions/claim) is empty so the measurement isolates the
    task-intrinsic packet body; the reduction is from the section-trimming the
    lightweight variant applies, which is the right-sizing lever.
    """
    from anvil.context.packets import _render_markdown, is_lightweight

    ctx: dict[str, Any] = {
        "feature": None,
        "dependencies_completed": [],
        "dependencies_open": [],
        "related_decisions": [],
        "active_claim": None,
    }
    full = _render_markdown(task, lightweight=False, **ctx)
    lite = _render_markdown(task, lightweight=True, **ctx)
    routed = "lightweight" if is_lightweight(task) else "full"
    return PacketSizing(
        task_id=task.id,
        routed_variant=routed,
        full_tokens=_token_estimate(full),
        lightweight_tokens=_token_estimate(lite),
    )


def measure_backlog(tasks: list[Task]) -> dict[str, Any]:
    """Aggregate packet-sizing metrics across a backlog.

    ``as_routed_*`` reflects the ACTUAL routing (lightweight tasks billed at
    their lightweight size, full tasks at full) — i.e. the real token saving the
    fast-lane already delivers — while ``avg_*`` shows the per-variant means.
    """
    rows = [measure_task(t) for t in tasks]
    n = len(rows)
    full_total = sum(r.full_tokens for r in rows)
    lite_total = sum(r.lightweight_tokens for r in rows)
    as_routed = sum(
        r.lightweight_tokens if r.routed_variant == "lightweight" else r.full_tokens
        for r in rows
    )
    return {
        "total_tasks": n,
        "routed_lightweight": sum(1 for r in rows if r.routed_variant == "lightweight"),
        "avg_full_tokens": round(full_total / n, 1) if n else 0.0,
        "avg_lightweight_tokens": round(lite_total / n, 1) if n else 0.0,
        "all_full_total_tokens": full_total,
        "as_routed_total_tokens": as_routed,
        "as_routed_savings_pct": (
            round(100.0 * (full_total - as_routed) / full_total, 1)
            if full_total
            else 0.0
        ),
        "per_task": [
            {
                "task_id": r.task_id,
                "routed_variant": r.routed_variant,
                "full_tokens": r.full_tokens,
                "lightweight_tokens": r.lightweight_tokens,
                "reduction_pct": r.reduction_pct,
            }
            for r in rows
        ],
    }


def _main(argv: list[str] | None = None) -> int:
    """Standalone entry: ``python -m anvil.context.packet_metrics <state_dir>``."""
    import json
    import sys
    from pathlib import Path

    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(
            "usage: python -m anvil.context.packet_metrics <state_dir>",
            file=sys.stderr,
        )
        return 2

    from anvil.clock import SystemClock
    from anvil.state.sqlite import SqliteBackend

    state_dir = Path(args[0]).expanduser()
    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(state_dir / "events.jsonl"),
        clock=SystemClock(),
    )
    backend.initialize()
    try:
        tasks = backend.list_tasks()
    finally:
        backend.close()
    print(json.dumps(measure_backlog(tasks), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
