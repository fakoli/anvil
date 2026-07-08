"""``anvil notify-digest`` — a one-line needs-review + blockers summary.

Built for OpenClaw Gateway cron (``cron add … --announce``): print a single line
ONLY when there is something to flag (tasks awaiting review or blocked), and NOTHING
otherwise — so a recurring ``--announce`` job stays silent on a quiet queue instead
of pinging a channel every interval. Always exits 0 (a notifier must never fail a
cron run); an uninitialized / unreadable project simply has nothing to announce.
``--json`` emits the counts as a machine-readable envelope.
"""

from __future__ import annotations

from pathlib import Path

import typer

from anvil.cli._helpers import _open_backend, _resolve_state_dir
from anvil.cli._json import JSON_OPTION, emit_success


def notify_digest(
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory to inspect. Defaults to the current working directory.",
    ),
) -> None:
    """One-line summary of tasks needing review + blockers; silent when there is
    nothing to report (so a cron ``--announce`` stays quiet on a clean queue)."""
    needs_review = blocked = expiring_soon = 0
    # A cron notifier must NEVER fail the run, whatever state the queue is in — an
    # uninitialized, mis-configured (bad ANVIL_ROOT), schema-mismatched, locked, or
    # even corrupt/truncated state.db all just mean "nothing to announce". Resolve +
    # read inside ONE broad guard and fall through to the zero case; a Gateway cron
    # must never see a stack trace (or a non-zero exit) from a status check.
    try:
        state_dir = _resolve_state_dir(cwd)
        if state_dir.exists():
            from anvil.cli._helpers import _load_config_optional
            from anvil.clock import SystemClock

            backend = _open_backend(state_dir)
            try:
                tasks = backend.list_tasks()
                active_claims = backend.list_active_claims()
            finally:
                backend.close()
            needs_review = sum(1 for t in tasks if t.status == "needs_review")
            blocked = sum(1 for t in tasks if t.status == "blocked")
            # retro-opps T008 — same threshold knob as the heartbeat hook's
            # in-session warning; this is the out-of-session (cron) channel.
            cfg = _load_config_optional(state_dir)
            warn_minutes = cfg.lease_warning_minutes if cfg is not None else 10.0
            if warn_minutes > 0:
                now = SystemClock().now()
                expiring_soon = sum(
                    1
                    for c in active_claims
                    if (c.lease_expires_at - now).total_seconds() / 60.0
                    < warn_minutes
                )
    except Exception:  # noqa: BLE001  (a notifier must never crash a cron run)
        needs_review = blocked = expiring_soon = 0

    if json_output:
        emit_success(
            "notify-digest",
            {
                "needs_review": needs_review,
                "blocked": blocked,
                "expiring_soon": expiring_soon,
                "total": needs_review + blocked,
            },
        )
        return

    # Print ONE line only when there's something to report. Staying silent at zero
    # keeps a recurring --announce cron from pinging a clean queue every interval.
    if needs_review or blocked or expiring_soon:
        parts = []
        if needs_review:
            parts.append(f"{needs_review} task(s) need review")
        if blocked:
            parts.append(f"{blocked} blocked")
        if expiring_soon:
            parts.append(f"{expiring_soon} lease(s) expiring soon")
        typer.echo(f"anvil: {' · '.join(parts)}")
