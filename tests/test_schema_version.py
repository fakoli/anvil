"""schema_version exposure tests (T007/B11).

The existing SCHEMA_VERSION (=7) is surfaced to tooling through:
- a public accessor ``schema.get_schema_version()``;
- a backend accessor ``SqliteBackend.get_schema_version()`` (the DB's stamped
  ``PRAGMA user_version``);
- the ``status`` command output (human line + ``--json`` data).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from anvil.cli import app
from anvil.state.schema import SCHEMA_VERSION, get_schema_version

runner = CliRunner()


def _invoke(tmp_path: Path, cmd: list[str]):  # type: ignore[no-untyped-def]
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        return runner.invoke(app, cmd, catch_exceptions=False)
    finally:
        os.chdir(original_cwd)


def _init(tmp_path: Path) -> None:
    res = _invoke(tmp_path, ["init", "--name", "Schema Version Project"])
    assert res.exit_code == 0, res.output


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def test_get_schema_version_matches_constant() -> None:
    """The public accessor returns the SCHEMA_VERSION constant (==7)."""
    assert get_schema_version() == SCHEMA_VERSION
    assert get_schema_version() == 7


def test_backend_get_schema_version_matches_constant(tmp_path: Path) -> None:
    """A freshly initialized DB stamps user_version == SCHEMA_VERSION."""
    from anvil.clock import SystemClock
    from anvil.state.sqlite import SqliteBackend

    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    (state_dir / "events.jsonl").touch()
    backend = SqliteBackend(
        db_path=str(state_dir / "state.db"),
        events_path=str(state_dir / "events.jsonl"),
        clock=SystemClock(),
    )
    backend.initialize()
    try:
        assert backend.get_schema_version() == SCHEMA_VERSION
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# status surfaces schema_version
# ---------------------------------------------------------------------------


def test_status_json_includes_schema_version(tmp_path: Path) -> None:
    """status --json data carries schema_version == SCHEMA_VERSION."""
    _init(tmp_path)
    res = _invoke(tmp_path, ["status", "--json"])
    assert res.exit_code == 0, res.output
    env = json.loads(res.stdout.strip())
    assert env["ok"] is True
    assert env["command"] == "status"
    assert env["data"]["schema_version"] == SCHEMA_VERSION
    # The DB-stamped version is surfaced too and matches on a healthy project.
    assert env["data"]["db_schema_version"] == SCHEMA_VERSION


def test_status_human_includes_schema_version(tmp_path: Path) -> None:
    """Human status output shows the Schema line with the version number."""
    _init(tmp_path)
    res = _invoke(tmp_path, ["status"])
    assert res.exit_code == 0, res.output
    assert "Schema:" in res.output
    assert str(SCHEMA_VERSION) in res.output


# ---------------------------------------------------------------------------
# read_db_schema_version reads the TRUE on-disk version without migrating
# ---------------------------------------------------------------------------


def _stamp_user_version(state_dir: Path, version: int) -> None:
    """Force PRAGMA user_version on the project's state.db (out of band)."""
    import sqlite3

    conn = sqlite3.connect(str(state_dir / "state.db"))
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def test_read_db_schema_version_returns_zero_for_missing_db(
    tmp_path: Path,
) -> None:
    """A non-existent db reports user_version 0 (SQLite's default)."""
    from anvil.state.sqlite import read_db_schema_version

    assert read_db_schema_version(str(tmp_path / "nope.db")) == 0


def test_read_db_schema_version_does_not_migrate(tmp_path: Path) -> None:
    """The standalone read reports the TRUE on-disk version, unmigrated."""
    from anvil.state.sqlite import read_db_schema_version

    _init(tmp_path)
    state_dir = tmp_path / ".anvil"
    _stamp_user_version(state_dir, 3)

    # Reads v3 (pre-migration) — NOT the code SCHEMA_VERSION.
    assert read_db_schema_version(str(state_dir / "state.db")) == 3
    # And the read is read-only: the on-disk version is untouched afterward.
    assert read_db_schema_version(str(state_dir / "state.db")) == 3


# ---------------------------------------------------------------------------
# MUST-FIX 2: status reports an un-migratable schema cleanly (no traceback)
# ---------------------------------------------------------------------------


def test_status_unknown_schema_version_clean_error_human(tmp_path: Path) -> None:
    """user_version=99 -> status exits 1 with a clean 'Error:' line, no traceback."""
    _init(tmp_path)
    _stamp_user_version(tmp_path / ".anvil", 99)

    # catch_exceptions defaults True here so an UNCAUGHT exception would surface
    # as res.exception (a traceback) rather than a clean exit — we assert none.
    res = _invoke(tmp_path, ["status"])
    assert res.exit_code == 1, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), (
        f"status raised a traceback: {res.exception!r}"
    )
    combined = res.output + (getattr(res, "stderr", "") or "")
    assert "Error:" in combined
    assert "99" in combined


def test_status_unknown_schema_version_clean_error_json(tmp_path: Path) -> None:
    """user_version=99 -> status --json returns a schema_mismatch envelope, exit 1."""
    _init(tmp_path)
    _stamp_user_version(tmp_path / ".anvil", 99)

    res = _invoke(tmp_path, ["status", "--json"])
    assert res.exit_code == 1, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), (
        f"status --json raised a traceback: {res.exception!r}"
    )
    env = json.loads(res.stdout.strip())
    assert env["ok"] is False
    assert env["command"] == "status"
    assert env["error"]["code"] == "schema_mismatch"
    assert env["error"]["message"]


def test_status_hook_format_unknown_schema_version_exits_zero(
    tmp_path: Path,
) -> None:
    """Hook safety: a bad schema must not fail the SessionStart hook."""
    _init(tmp_path)
    _stamp_user_version(tmp_path / ".anvil", 99)

    res = _invoke(tmp_path, ["status", "--hook-format"])
    assert res.exit_code == 0, res.output
    assert res.output.strip() == "uninitialized"
