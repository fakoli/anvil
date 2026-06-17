"""hook sub-app: check-claim, record-file-change, capture-evidence.

Internal helpers invoked by the plugin's bash hooks.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from fakoli_state.cli._helpers import (
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
        from fakoli_state.clock import SystemClock as _SystemClock
        from fakoli_state.state.sqlite import SqliteBackend as _SqliteBackend

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
                        f"[fakoli-state:check-claim] WARNING: file '{file}' is "
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
        from fakoli_state.clock import SystemClock as _SystemClock
        from fakoli_state.state.models import EventDraft as _EventDraft
        from fakoli_state.state.sqlite import SqliteBackend as _SqliteBackend

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
    """Append a verification-command capture to .fakoli-state/.evidence-buffer/.

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

        # Read stdout/stderr excerpts from temp files (up to 4000 chars each).
        stdout_excerpt = ""
        if stdout_file is not None:
            try:
                stdout_excerpt = stdout_file.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                pass

        stderr_excerpt = ""
        if stderr_file is not None:
            try:
                stderr_excerpt = stderr_file.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                pass

        # Build the evidence record.
        now = datetime.datetime.now(datetime.UTC)
        record: dict[str, object] = {
            "timestamp": now.isoformat(),
            "command": command,
            "exit_code": exit_code,
            "stdout_excerpt": stdout_excerpt,
            "stderr_excerpt": stderr_excerpt,
            "actor": actor,
        }

        # Determine which buffer file to append to by looking up the active claim.
        buffer_dir = state_dir / ".evidence-buffer"
        buffer_dir.mkdir(exist_ok=True)

        claim_id: str | None = None
        try:
            from fakoli_state.clock import SystemClock as _SystemClock
            from fakoli_state.state.sqlite import SqliteBackend as _SqliteBackend

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
                "pass this file via: fakoli-state submit TASK_ID --output-file <THIS_FILE>"
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
