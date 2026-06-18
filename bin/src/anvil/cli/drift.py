"""``anvil drift`` — read-only divergence report (backlog T026/B26, P1).

Surfaces anvil's three-source reconciliation as a first-class,
queryable DRIFT VIEW: the answer to "is the code still what the spec/plan
said?". It reports divergence between

* **INTENT**     — what the plan/tasks declared (``Task.likely_files``).
* **STATE**      — the SQLite task / claim records.
* **FILESYSTEM / GIT** — what actually exists on disk: agent branches,
  worktrees, work packets, and the expected files themselves.

Relationship to ``sync``
------------------------
``drift`` is a strict subset of the *bare* ``sync`` reconciliation pass —
it reuses the very same :class:`ReconciliationEngine` and calls
``engine.scan()`` (NEVER ``fix()`` — it cannot mutate, unlike
``sync --fix``). The two differences are deliberate:

1. **No provider required.** ``sync`` resolves configured providers and can
   emit ``missing_sync_mapping`` / ``drift_sync_state`` discrepancies, which
   only mean anything when a GitHub/Linear/… provider is configured. ``drift``
   passes NO providers and filters the report down to
   :data:`LOCAL_DRIFT_KINDS` — the local intent/state/fs/git drift that every
   project has regardless of external integrations. So ``drift`` works from a
   fresh ``init`` with zero sync config.
2. **Read-only by contract.** ``sync`` is a mutation surface (``--fix``,
   ``--push``, ``--pull``). ``drift`` has no such flags; it is a report.

Exit code
---------
``drift`` exits **0 whether or not drift is found** — it is a report, not a
gate. (Compare ``sync``'s manual-merge exit 2.) A CI job that wants drift to
fail the build can parse ``--json`` and branch on ``data.summary.total``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from anvil.cli._helpers import (
    StateRootError,
    _open_backend,
    _require_state_dir,
    _resolve_state_dir,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail

if TYPE_CHECKING:
    from anvil.sync.reconciliation import Discrepancy, ReconciliationReport

__all__ = ["drift"]

_COMMAND = "drift"


def drift(
    json_output: bool = JSON_OPTION,
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Report drift between intent (plan), state (SQLite), and filesystem/git.

    Read-only. Reuses the reconciliation engine in scan/report mode and shows
    only LOCAL drift (no external sync provider required): orphan branches,
    orphan worktrees, orphan packets, stale claims, and tasks whose
    plan-declared files have vanished from disk. Always exits 0 — this is a
    report, not a gate.
    """
    # MUST-FIX 3 (pipe-safe --json): _resolve_state_dir raises StateRootError
    # (a ClickException) when ANVIL_ROOT is set but invalid. Under
    # --json that error would reach stdout/stderr as a raw `Error: ...` line —
    # NOT a parseable envelope — breaking any consumer doing json.load(stdout).
    # Mirror the v1.23.7 `status` handling: emit the standard error envelope
    # {"ok": false, "command": "drift", "error": {"code": ...}} to stdout with
    # a non-zero exit. Without --json the ClickException propagates unchanged
    # (clean human `Error:` line on stderr, exit 1).
    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError as exc:
        if json_output:
            fail(_COMMAND, str(exc), code="state_root_invalid")
        raise
    _require_state_dir(state_dir, command=_COMMAND, json_output=json_output)
    backend = _open_backend(state_dir)
    try:
        items = _collect_local_drift(backend, state_dir)
    finally:
        backend.close()

    if json_output:
        emit_success(_COMMAND, _build_json_data(items))
        return

    _print_human(items)


# ---------------------------------------------------------------------------
# Collection — reuse the reconciliation engine, filter to local drift
# ---------------------------------------------------------------------------


def _collect_local_drift(backend: Any, state_dir: Path) -> list[Discrepancy]:
    """Run ``ReconciliationEngine.scan()`` and keep only local drift kinds.

    Crucially this passes ``configured_providers=[]`` so the scan never
    produces the provider-dependent kinds, then filters the result to
    :data:`LOCAL_DRIFT_KINDS` as a belt-and-braces guard. Order is preserved
    from the engine (kind ASC, target_id ASC) so output is deterministic.
    """
    from anvil.clock import SystemClock
    from anvil.sync.reconciliation import (
        LOCAL_DRIFT_KINDS,
        ReconciliationEngine,
    )

    engine = ReconciliationEngine(
        backend,
        state_dir=state_dir,
        clock=SystemClock(),
        # No providers: drift is local-only and must work without a
        # configured GitHub/Linear/etc. integration.
        configured_providers=[],
    )
    report: ReconciliationReport = engine.scan()
    return [d for d in report.discrepancies if d.kind in LOCAL_DRIFT_KINDS]


# ---------------------------------------------------------------------------
# JSON envelope payload
# ---------------------------------------------------------------------------


def _build_json_data(items: list[Discrepancy]) -> dict[str, Any]:
    """Build the ``data`` object for the v1.23.4 success envelope.

    Shape::

        {
          "drift": [
            {
              "category": "missing_expected_file",
              "severity": "warning",
              "description": "...",
              "task": "T001" | null,
              "file": "src/foo.py" | null,
              "path": ".../wt-t099" | null,
              "branch": "agent/t001-x" | null,
              "target_kind": "task",
              "target_id": "T001:src/foo.py"
            },
            ...
          ],
          "summary": {
            "total": <int>,
            "by_category": {"<category>": <int>, ...}
          }
        }
    """
    drift_items = [_item_to_json(d) for d in items]
    by_category: dict[str, int] = {}
    for d in items:
        by_category[str(d.kind)] = by_category.get(str(d.kind), 0) + 1
    return {
        "drift": drift_items,
        "summary": {"total": len(items), "by_category": by_category},
    }


def _item_to_json(d: Discrepancy) -> dict[str, Any]:
    """Serialize one discrepancy into the drift-item wire shape.

    Pulls the involved task / file / path / branch out of the discrepancy
    payload so consumers don't have to reverse-engineer them from
    ``target_id``.

    SHOULD-FIX: ``file`` is reserved for genuine FILE targets — it is sourced
    ONLY from ``expected_file`` (the ``missing_expected_file`` check). The old
    ``expected_file or path`` fallback mislabelled an ``orphan_worktree``'s
    worktree DIRECTORY as a ``file``. Non-file filesystem targets (a worktree
    directory, a packet file) are carried in the separate ``path`` field, so a
    consumer can tell "a file the plan promised is gone" apart from "a
    leftover directory/packet on disk". ``file`` is ``null`` for worktrees;
    ``path`` is ``null`` for ``missing_expected_file`` and ``orphan_branch``.
    """
    payload = d.payload or {}
    return {
        "category": str(d.kind),
        "severity": str(d.severity),
        "description": d.description,
        "task": payload.get("task_id"),
        "file": payload.get("expected_file"),
        "path": payload.get("path"),
        "branch": payload.get("branch"),
        "target_kind": d.target_kind,
        "target_id": d.target_id,
    }


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------


def _print_human(items: list[Discrepancy]) -> None:
    """Render a readable summary; empty drift prints the canonical line."""
    if not items:
        typer.echo("No drift detected.")
        return

    typer.echo(f"Detected {len(items)} drift item(s):")
    typer.echo("")
    for d in items:
        typer.echo(f"  [{d.severity}] {d.kind}: {d.target_id}")
        typer.echo(f"      {d.description}")
    typer.echo("")
    typer.echo("Summary:")
    by_category: dict[str, int] = {}
    for d in items:
        by_category[str(d.kind)] = by_category.get(str(d.kind), 0) + 1
    for category, count in sorted(by_category.items()):
        typer.echo(f"  {category}: {count}")
