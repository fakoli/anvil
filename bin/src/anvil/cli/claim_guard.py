"""``anvil claim-guard`` — before-edit claim check for native harnesses (B42 Phase 2 item 3).

Read-only. Answers, for an agent about to run a MUTATING tool: does this actor
hold an active claim, and does it cover the file(s) being edited? Built for
OpenClaw's native ``before_tool_call`` hook (anvil's claim-guard): the node plugin
shells out to ``anvil claim-guard --json --actor agent --file <p> --cwd <dir>``
and maps the verdict to allow / log-warn / requireApproval / hard-block per its
configured mode. anvil writes nothing for OpenClaw — this only reads state.

Verdict — the verb is **mode-agnostic**; the plugin decides what to DO with it:

* not a tracked project / state unavailable / read error  → continue (exit 0)
* actor holds NO active claim                              → block (exit 2) [escalation-eligible]
* actor's claim does not cover the edited file(s)          → warn (exit 0) [advisory]
* actor holds a covering claim (or no files given)         → continue (exit 0)

DEFAULT-OPEN like ``gate-check``: only the has-NO-claim case yields exit 2; every
uncertain path allows. "Outside scope" is advisory (warn, exit 0) because
``Claim.expected_files`` is not exhaustive — file-level mismatch must never hard-block.

Exit codes (so a jq-less host can branch on ``$?``)::

    0 = allow (continue/warn)    2 = block (no claim)    1 = genuine error
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from anvil.cli._helpers import (
    StateRootError,
    _open_backend,
    _resolve_state_dir,
    resolve_actor,
)
from anvil.cli._json import JSON_OPTION, emit_success, fail

if TYPE_CHECKING:
    from anvil.state.models import Claim

__all__ = ["claim_guard"]

_COMMAND = "claim-guard"


def claim_guard(
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help="Actor whose active claims to check. Defaults to $USER or 'agent'.",
    ),
    files: list[str] = typer.Option(  # noqa: B008
        [],
        "--file",
        help="Path(s) the tool is about to mutate (repeatable). Used for scope (warn) only.",
    ),
    json_output: bool = JSON_OPTION,
    quiet: bool = typer.Option(  # noqa: B008
        False,
        "-q",
        "--quiet",
        help="Exit-code only (0 allow / 2 block); print nothing to stdout.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Check whether an actor may mutate, or must claim a task first.

    Read-only. Default-open: only blocks (exit 2) when *this actor* holds NO
    active claim; a claim that doesn't cover the file(s) only warns (exit 0).
    """
    resolved_actor = resolve_actor(actor)
    file_list = [_normalize(f) for f in (files or [])]

    try:
        state_dir = _resolve_state_dir(cwd)
    except StateRootError as exc:
        if json_output:
            fail(_COMMAND, str(exc), code="state_root_invalid")
        raise

    if not state_dir.exists():
        _emit(json_output, quiet, _allow(
            resolved_actor, file_list, "no_project",
            "Not a tracked anvil project; allowed.",
        ))
        return

    try:
        backend = _open_backend(state_dir)
    except Exception:  # noqa: BLE001 — open failure ⇒ cannot gate ⇒ allow
        _emit(json_output, quiet, _allow(
            resolved_actor, file_list, "state_unavailable",
            "anvil state unavailable; allowed.",
        ))
        return

    # Read guarded (default-open on a read fault); the pure decision runs outside
    # the guard so a wrong predicate still surfaces in tests.
    try:
        claims = _actor_claims(backend, resolved_actor)
    except Exception:  # noqa: BLE001 — read fault ⇒ allow, never block on a db hiccup
        claims = None
    finally:
        backend.close()

    if claims is None:
        _emit(json_output, quiet, _allow(
            resolved_actor, file_list, "state_unavailable",
            "anvil state unavailable; allowed.",
        ))
        return

    if not claims:
        # The only escalation-eligible verdict: editing with no claim at all.
        _emit(json_output, quiet, {
            "block": True,
            "action": "block",
            "actor": resolved_actor,
            "files": file_list,
            "has_claim": False,
            "covered": False,
            "claim": None,
            "scope": "no_claim",
            "reason": (
                f"'{resolved_actor}' is about to edit without an active anvil claim. "
                f"Claim a task first: `anvil next` then `anvil claim <task-id>`."
            ),
        })
        return

    claim_id = claims[0].id
    covered: set[str] = set()
    for claim in claims:
        covered |= {_normalize(f) for f in claim.expected_files}
    uncovered = [f for f in file_list if f not in covered]

    # Only warn "outside scope" when the claim actually DECLARES a scope. An empty
    # expected_files means scope is unknown, not "covers nothing" — warning on every
    # edit would be pure noise.
    if file_list and covered and uncovered:
        _emit(json_output, quiet, {
            "block": False,
            "action": "warn",
            "actor": resolved_actor,
            "files": file_list,
            "has_claim": True,
            "covered": False,
            "claim": claim_id,
            "scope": "outside_scope",
            "reason": (
                f"Editing files outside your claimed task's declared scope: "
                f"{', '.join(uncovered)}. (Advisory — expected_files is not exhaustive.)"
            ),
        })
        return

    _emit(json_output, quiet, {
        "block": False,
        "action": "continue",
        "actor": resolved_actor,
        "files": file_list,
        "has_claim": True,
        "covered": True,
        "claim": claim_id,
        "scope": "covered" if file_list else "no_files",
        "reason": "Actor holds a covering active claim; allowed.",
    })


def _actor_claims(backend: Any, actor: str) -> list[Claim]:
    """Active claims held by *actor* (the guard only judges this actor)."""
    return [c for c in backend.list_active_claims() if c.claimed_by == actor]


def _normalize(path: str) -> str:
    """Normalize a path for loose scope comparison (strip leading ./ segments)."""
    p = path.strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _allow(actor: str, files: list[str], scope: str, reason: str) -> dict[str, Any]:
    """A no-block (allow) decision payload — the default-open cases."""
    return {
        "block": False,
        "action": "continue",
        "actor": actor,
        "files": files,
        "has_claim": False,
        "covered": True,
        "claim": None,
        "scope": scope,
        "reason": reason,
    }


def _emit(json_output: bool, quiet: bool, decision: dict[str, Any]) -> None:
    """Emit the decision and set the exit code (0 allow / 2 block)."""
    block = decision["block"]
    if quiet:
        if block:
            raise typer.Exit(code=2)
        return
    if json_output:
        emit_success(_COMMAND, decision)
    else:
        typer.echo(decision["reason"])
    if block:
        raise typer.Exit(code=2)
