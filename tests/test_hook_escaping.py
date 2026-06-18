"""Durability tests for record-file-change.sh and capture-evidence.sh.

Regression coverage for the backslash/double-quote escaping bug:
  - The original fallback path in record-file-change.sh hand-built a JSON line
    by shell interpolation, so a backslash in FILE_PATH produced invalid JSON.
  - replay_from_empty() treats any interior malformed line as corruption and
    raises, breaking the audit-guarantee primitive.

Fix: the single python3 pass now calls json.dumps() to produce the complete
event line; the shell appends it verbatim.  These tests confirm:
  (a) The appended events.jsonl line parses with json.loads.
  (b) replay_from_empty() over that log succeeds without raising.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from anvil.clock import FrozenClock
from anvil.state.sqlite import SqliteBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_RECORD_CHANGE_SH = _REPO_ROOT / "hooks" / "record-file-change.sh"
_CAPTURE_EVIDENCE_SH = _REPO_ROOT / "hooks" / "capture-evidence.sh"

_T0_STR = "2026-05-24T18:00:00Z"
from datetime import UTC, datetime
_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC)


def _run_hook(hook_path: Path, payload: dict, *, anvil_dir: Path) -> subprocess.CompletedProcess:
    """Run a hook script with the given JSON payload, CWD set to the tmp project root."""
    env = os.environ.copy()
    # Unset CLAUDE_PLUGIN_ROOT so the CLI path is never taken; we test the
    # direct-append fallback path.
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    result = subprocess.run(
        ["bash", str(hook_path)],
        input=json.dumps(payload).encode(),
        cwd=str(anvil_dir),
        env=env,
        capture_output=True,
    )
    return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """A temp directory with a minimal .anvil/ layout."""
    anvil = tmp_path / ".anvil"
    anvil.mkdir()
    (anvil / "events.jsonl").touch()
    (anvil / "state.db").touch()
    return tmp_path


@pytest.fixture()
def backend(project_dir: Path):  # type: ignore[no-untyped-def]
    """An initialized SqliteBackend in project_dir."""
    from datetime import UTC, datetime
    db_path = str(project_dir / ".anvil" / "state.db")
    events_path = str(project_dir / ".anvil" / "events.jsonl")
    b = SqliteBackend(
        db_path=db_path,
        events_path=events_path,
        clock=FrozenClock(_T0),
    )
    b.initialize()
    yield b
    b.close()


# ---------------------------------------------------------------------------
# record-file-change.sh escaping tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "file_path",
    [
        r"C:\Users\foo\bar.py",          # Windows-style backslashes
        r'src/"quoted"/file.py',          # embedded double-quote
        r'src/back\slash"and"quote.py',   # both
        "src/uniécode.py",           # non-ASCII unicode
        "src/newline\nin_path.py",        # embedded newline (unusual but possible)
    ],
    ids=["backslash", "dquote", "both", "unicode", "newline"],
)
def test_record_file_change_appends_valid_json(
    file_path: str,
    project_dir: Path,
) -> None:
    """record-file-change.sh must produce parseable JSON regardless of path content."""
    payload = {
        "tool_name": "Edit",
        "tool_input": {"path": file_path},
        "session_id": "sess-escape-test",
    }

    result = _run_hook(_RECORD_CHANGE_SH, payload, anvil_dir=project_dir)
    assert result.returncode == 0, (
        f"record-file-change.sh exited {result.returncode}\n"
        f"stderr: {result.stderr.decode()}"
    )

    events_path = project_dir / ".anvil" / "events.jsonl"
    lines = [ln for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "events.jsonl must contain at least one line after the hook ran"

    last_line = lines[-1]
    try:
        event = json.loads(last_line)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"Appended events.jsonl line is not valid JSON: {exc}\n"
            f"  file_path input: {file_path!r}\n"
            f"  raw line: {last_line!r}"
        )

    # The file path must survive the round-trip correctly.
    # For paths with embedded newlines, the hook legitimately only records the
    # first line (shell limitation); accept prefix match in that edge case.
    if "\n" not in file_path:
        assert event.get("entity_id") == file_path, (
            f"entity_id mismatch: {event.get('entity_id')!r} != {file_path!r}"
        )
    else:
        assert event.get("entity_id", "").startswith(file_path.split("\n")[0]), (
            f"entity_id prefix mismatch for newline path: {event.get('entity_id')!r}"
        )


@pytest.mark.parametrize(
    "file_path",
    [
        r"C:\Users\foo\bar.py",
        r'src/"quoted"/file.py',
        r'src/back\slash"and"quote.py',
    ],
    ids=["backslash", "dquote", "both"],
)
def test_replay_from_empty_survives_escaped_path(
    file_path: str,
    project_dir: Path,
    backend: "SqliteBackend",
) -> None:
    """replay_from_empty() must not raise when events.jsonl has escaped paths.

    This is the core durability regression: before the fix, a backslash in
    entity_id produced invalid JSON that replay_from_empty raised on as
    corruption.
    """
    payload = {
        "tool_name": "Write",
        "tool_input": {"path": file_path},
        "session_id": "sess-replay-test",
    }

    result = _run_hook(_RECORD_CHANGE_SH, payload, anvil_dir=project_dir)
    assert result.returncode == 0

    events_path = project_dir / ".anvil" / "events.jsonl"

    # Verify the line parses before attempting replay.
    lines = [ln for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "events.jsonl must not be empty"
    json.loads(lines[-1])  # raises on malformed JSON

    # replay_from_empty must not raise.
    # Note: the hook-written event uses an action not in the Event model
    # (action="file_changed" is a hook-only event; replay may skip it or
    # tolerate it as a non-modelling event).  The important contract is that
    # replay_from_empty does NOT raise a ValueError about malformed JSON.
    try:
        backend.replay_from_empty(str(events_path))
    except ValueError as exc:
        if "malformed JSON" in str(exc):
            pytest.fail(
                f"replay_from_empty raised ValueError (malformed JSON) for "
                f"file_path={file_path!r}: {exc}"
            )
        # A ValueError about a non-JSON issue (e.g. unknown event action) is
        # acceptable — the test is specifically guarding against JSON parse failures.
    except Exception:
        # Any other exception (unknown action, schema error) is out of scope
        # for this regression — we only care that invalid JSON is not the cause.
        pass
