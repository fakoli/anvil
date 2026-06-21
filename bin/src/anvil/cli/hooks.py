"""hook sub-app: check-claim, record-file-change, capture-evidence.

Internal helpers invoked by the plugin's bash hooks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from anvil.cli._helpers import (
    _resolve_state_dir,
)

hook_app = typer.Typer(
    name="hook",
    help="Internal hook helpers — invoked by the plugin's bash hooks.",
    no_args_is_help=True,
)


@hook_app.command("check-claim")
def hook_check_claim(
    file: str = typer.Option(..., "--file", help="Path of the file about to be modified."),  # noqa: B008,A002
    actor: str = typer.Option(..., "--actor", help="Session actor / session_id."),  # noqa: B008
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Used by hooks/check-claim.sh — exit 0 always; output goes to stderr.

    Checks whether FILE is within the scope of an active claim.
    - If FILE is in expected_files of a claim by THIS actor: silent exit 0.
    - If FILE is in expected_files of a claim by ANOTHER actor: warn to stderr.
    - If no active claims exist: silent exit 0.
    """
    # Defer all imports inside the body — this hook fires on every file edit,
    # so startup latency is the primary concern.
    try:
        from anvil.clock import SystemClock as _SystemClock
        from anvil.state.sqlite import SqliteBackend as _SqliteBackend

        state_dir = _resolve_state_dir(cwd)
        if not state_dir.exists():
            raise typer.Exit(code=0)

        db_path = str(state_dir / "state.db")
        events_path = str(state_dir / "events.jsonl")
        backend = _SqliteBackend(
            db_path=db_path,
            events_path=events_path,
            clock=_SystemClock(),
        )
        backend.initialize()
        try:
            active_claims = backend.list_active_claims()
        finally:
            backend.close()

        if not active_claims:
            raise typer.Exit(code=0)

        normalized = file.lstrip("./")
        for active_claim in active_claims:
            # Normalize expected_files the same way for comparison.
            claim_files = {f.lstrip("./") for f in active_claim.expected_files}
            if normalized in claim_files or file in claim_files:
                if active_claim.claimed_by != actor:
                    typer.echo(
                        f"[anvil:check-claim] WARNING: file '{file}' is "
                        f"in the scope of claim '{active_claim.id}' owned by "
                        f"'{active_claim.claimed_by}', not '{actor}'.",
                        err=True,
                    )
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass  # hook must never block the tool
    raise typer.Exit(code=0)


@hook_app.command("record-file-change")
def hook_record_file_change(
    file: str = typer.Option(..., "--file", help="Path of the file that was modified."),  # noqa: B008,A002
    tool: str = typer.Option(..., "--tool", help="Tool name (Edit, Write, NotebookEdit)."),  # noqa: B008
    actor: str = typer.Option(..., "--actor", help="Session actor / session_id."),  # noqa: B008
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Used by hooks/record-file-change.sh — appends a file_changed event.

    Writes a file_changed event to both the SQLite events table and events.jsonl.
    Exits 0 always; any failure is silently swallowed so the hook never blocks
    the tool that triggered it.
    """
    # Defer all imports — this hook fires on every file write; keep startup fast.
    try:
        from anvil.clock import SystemClock as _SystemClock
        from anvil.state.models import EventDraft as _EventDraft
        from anvil.state.sqlite import SqliteBackend as _SqliteBackend

        state_dir = _resolve_state_dir(cwd)
        if not state_dir.exists():
            raise typer.Exit(code=0)

        db_path = str(state_dir / "state.db")
        events_path = str(state_dir / "events.jsonl")
        clock = _SystemClock()
        backend = _SqliteBackend(
            db_path=db_path,
            events_path=events_path,
            clock=clock,
        )
        backend.initialize()
        try:
            now = clock.now()
            draft = _EventDraft(
                timestamp=now,
                actor=actor or "hook",
                action="file_changed",
                target_kind="file",
                target_id=file,
                payload_json={
                    "file": file,
                    "tool": tool,
                    "actor": actor,
                    "changed_at": now.isoformat(),
                },
            )
            backend.append(draft)
        finally:
            backend.close()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass  # hook must never block the tool
    raise typer.Exit(code=0)


@hook_app.command("capture-evidence")
def hook_capture_evidence(
    command: str = typer.Option(..., "--command", help="Full bash command string that was run."),  # noqa: B008
    exit_code: int = typer.Option(..., "--exit-code", help="Exit code of the command."),  # noqa: B008
    stdout_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--stdout-file",
        help="Path to a temp file containing the command's stdout.",
    ),
    stderr_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--stderr-file",
        help="Path to a temp file containing the command's stderr.",
    ),
    actor: str = typer.Option(..., "--actor", help="Session actor / session_id."),  # noqa: B008
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """Append a verification-command capture to .anvil/.evidence-buffer/.

    Called by hooks/capture-evidence.sh after every bash tool invocation.
    Failures are swallowed — this hook must never break the session.
    Always exits 0.
    """
    # All failures are silently swallowed — hook must never break the session.
    try:
        import datetime

        state_dir = _resolve_state_dir(cwd)
        if not state_dir.exists():
            raise typer.Exit(code=0)

        import hashlib

        # Read FULL stdout/stderr from temp files. The output hash is over the
        # full output (before truncation) so output_sha256 records what actually
        # ran, not a truncated excerpt (SL-3 / B48). The 4000-char excerpts are
        # kept only as human-readable descriptive metadata.
        stdout_raw = ""
        if stdout_file is not None:
            try:
                stdout_raw = stdout_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

        stderr_raw = ""
        if stderr_file is not None:
            try:
                stderr_raw = stderr_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

        output_sha256 = hashlib.sha256(
            (stdout_raw + stderr_raw).encode("utf-8")
        ).hexdigest()

        # Build the evidence record — a CommandProof-shaped buffer line that
        # ``anvil submit`` reconciles into Evidence.proofs. ``kind`` +
        # ``output_sha256`` are what make it a typed, observed proof.
        now = datetime.datetime.now(datetime.UTC)
        record: dict[str, object] = {
            "kind": "command",
            "timestamp": now.isoformat(),
            "command": command,
            "exit_code": exit_code,
            "output_sha256": output_sha256,
            "stdout_excerpt": stdout_raw[:4000],
            "stderr_excerpt": stderr_raw[:4000],
            "actor": actor,
        }

        # Determine which buffer file to append to by looking up the active claim.
        buffer_dir = state_dir / ".evidence-buffer"
        buffer_dir.mkdir(exist_ok=True)

        claim_id: str | None = None
        try:
            from anvil.clock import SystemClock as _SystemClock
            from anvil.state.sqlite import SqliteBackend as _SqliteBackend

            db_path = str(state_dir / "state.db")
            events_path = str(state_dir / "events.jsonl")
            _backend = _SqliteBackend(
                db_path=db_path,
                events_path=events_path,
                clock=_SystemClock(),
            )
            _backend.initialize()
            try:
                for active_claim in _backend.list_active_claims():
                    if active_claim.claimed_by == actor:
                        claim_id = active_claim.id
                        break
            finally:
                _backend.close()
        except Exception:  # noqa: BLE001
            pass  # if the DB is unavailable, fall through to orphan

        if claim_id is not None:
            buffer_file = buffer_dir / f"{claim_id}.json"
        else:
            # No active claim found — write to orphan buffer. Recovery path
            # uses the existing `submit --output-file` flag; the previously-
            # referenced `evidence attach` subcommand did not exist (Critic-2
            # flagged that following the error message produced Typer's
            # "No such command 'evidence'" error).
            record["note"] = (
                "orphan — no active claim found at capture time; "
                "pass this file via: anvil submit TASK_ID --output-file <THIS_FILE>"
            )
            buffer_file = buffer_dir / "orphan.json"

        # Append the JSON record as a single line (JSONL).
        with buffer_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass  # hook must never block the session

    raise typer.Exit(code=0)


@hook_app.command("stop-gate")
def hook_stop_gate(
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help="Actor whose active claims to gate. Defaults to $ANVIL_GATE_ACTOR or 'agent'.",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the Stop payload's cwd, then the current dir.",
        hidden=True,
    ),
) -> None:
    """Stop-hook EVIDENCE GATE for Codex / Claude Code (B41 — OPT-IN).

    The Codex/Claude analogue of the OpenClaw before_agent_finalize finish-gate:
    when the turn ends with a claimed anvil task that has no submitted verification
    evidence, emit ``{"decision":"block","reason":...}`` on stdout AND exit 2 (a
    continuation prompt on stderr too) to force one more pass; otherwise exit 0.

    NOT wired by default — anvil's bundled hooks are non-blocking by design
    (docs/design.md). Opt in by adding a Stop hook that runs ``anvil hook
    stop-gate`` (see docs/reference/codex.md), and trust it via ``/hooks``. Reuses
    ``gate-check``'s decision logic; default-OPEN on every uncertain path; loop-
    guarded via the payload's ``stop_hook_active``.
    """
    import sys

    # Best-effort parse of the Stop payload on stdin (stop_hook_active, cwd).
    payload: dict[str, object] = {}
    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        if raw.strip():
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                payload = loaded
    except Exception:  # noqa: BLE001 — a malformed payload must not break the turn
        payload = {}

    # Loop guard: already inside a continuation we requested — never re-block.
    if payload.get("stop_hook_active"):
        raise typer.Exit(code=0)

    resolved_actor = actor or os.environ.get("ANVIL_GATE_ACTOR") or "agent"
    payload_cwd = payload.get("cwd")
    resolved_cwd = cwd
    if resolved_cwd is None and isinstance(payload_cwd, str) and payload_cwd:
        resolved_cwd = Path(payload_cwd)

    # Default-OPEN: any resolution/read failure ⇒ allow the turn to end (exit 0).
    try:
        from anvil.cli._helpers import _open_backend
        from anvil.cli.gate_check import _read_actor_rows, decide_from_rows

        state_dir = _resolve_state_dir(resolved_cwd)
        if not state_dir.exists():
            raise typer.Exit(code=0)
        backend = _open_backend(state_dir)
        try:
            rows = _read_actor_rows(backend, resolved_actor)
        finally:
            backend.close()
        decision = decide_from_rows(resolved_actor, rows)
    except typer.Exit:
        raise
    except Exception:  # noqa: BLE001 — never break the turn on an anvil error
        raise typer.Exit(code=0) from None

    if not decision.get("block"):
        raise typer.Exit(code=0)

    reason = str(decision.get("instruction") or "Submit verification evidence before finishing.")
    # Emit BOTH contracts: Codex/Claude honor {"decision":"block","reason":...} on
    # stdout; the exit-2-with-stderr-reason path is the fallback for harnesses that
    # read the continuation prompt from stderr.
    typer.echo(json.dumps({"decision": "block", "reason": reason}))
    typer.echo(reason, err=True)
    raise typer.Exit(code=2)


@hook_app.command("heartbeat")
def hook_heartbeat(
    actor: str | None = typer.Option(  # noqa: B008
        None,
        "--actor",
        help="Actor whose claim lease(s) to renew (default $ANVIL_GATE_ACTOR or 'agent').",
    ),
    cwd: Path | None = typer.Option(  # noqa: B008
        None,
        "--cwd",
        help="Project directory. Defaults to the current working directory.",
        hidden=True,
    ),
) -> None:
    """PostToolUse lease HEARTBEAT (B41) — renew the actor's active claim lease(s)
    on tool activity so a lazy lease stays fresh while real work is happening.

    Purely side-effecting and non-blocking: always exits 0, swallows every error
    (an expired lease raises — that is fine, the next claim/reclaim handles it).
    Wired into the bundled PostToolUse hooks (cross-harness, Claude + Codex).
    """
    resolved_actor = actor or os.environ.get("ANVIL_GATE_ACTOR") or "agent"
    try:
        from anvil.claims.manager import ClaimManager
        from anvil.cli._helpers import (
            _lease_manager_kwargs,
            _load_config_optional,
            _open_backend,
        )
        from anvil.clock import SystemClock

        state_dir = _resolve_state_dir(cwd)
        if not state_dir.exists():
            raise typer.Exit(code=0)
        clock = SystemClock()
        backend = _open_backend(state_dir)
        try:
            cfg = _load_config_optional(state_dir)
            lease_kwargs = _lease_manager_kwargs(cfg, lease_override=None)
            claim_ids = [
                c.id for c in backend.list_active_claims() if c.claimed_by == resolved_actor
            ]
            for claim_id in claim_ids:
                try:
                    manager = ClaimManager(backend, clock, actor=resolved_actor, **lease_kwargs)
                    manager.renew(claim_id)
                except Exception:  # noqa: BLE001 — expired/contended lease: skip, not fatal
                    pass
        finally:
            backend.close()
    except typer.Exit:
        raise
    except Exception:  # noqa: BLE001 — a heartbeat must never break the session
        pass

    raise typer.Exit(code=0)
