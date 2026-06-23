"""T018 — the unified PRD-partition resolver (``resolve_prd_id``).

One resolver shared by the CLI (``cli._helpers.resolve_prd_id``, behind the
shared ``PRD_OPTION`` flag) and the MCP server (``mcp_server._resolve_prd_id``),
so both surfaces pick the IDENTICAL PRD partition for identical DB + env inputs.

Precedence: explicit arg/flag (--prd) > $ANVIL_PRD > single PRD | default PRD |
ambiguity-error.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from anvil.cli._helpers import (
    PRD_OPTION,
    PrdAmbiguityError,
    resolve_prd_id,
)
from anvil.clock import FrozenClock
from anvil.mcp_server import _resolve_prd_id as mcp_resolve_prd_id
from anvil.state.sqlite import SqliteBackend

_PRD_ENV = "ANVIL_PRD"
_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC)


def _make_backend(state_dir: Path) -> SqliteBackend:
    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    Path(events_path).touch()
    b = SqliteBackend(
        db_path=db_path, events_path=events_path, clock=FrozenClock(_T0)
    )
    b.initialize()
    return b


def _insert_prd_raw(
    conn: sqlite3.Connection,
    *,
    prd_id: str,
    status: str = "draft",
    is_default: int = 0,
) -> None:
    """Raw-insert a prds row (matches tests/test_claims.py::_insert_prd_raw)."""
    conn.execute(
        "INSERT INTO prds (id, project_id, status, is_default) "
        "VALUES (?, 'proj-1', ?, ?)",
        (prd_id, status, is_default),
    )
    conn.commit()


def _insert_feature_raw(conn: sqlite3.Connection, feat_id: str = "F001") -> None:
    """Raw-insert a features row (tasks.feature_id FK; matches
    tests/test_claims.py::_insert_feature_raw)."""
    conn.execute(
        "INSERT OR IGNORE INTO features "
        "(id, title, description, status, requirements, tasks) "
        "VALUES (?, ?, 'desc', 'proposed', '[]', '[]')",
        (feat_id, f"Feature {feat_id}"),
    )
    conn.commit()


def _insert_task_raw(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    status: str = "ready",
    prd_id: str = "default",
) -> None:
    """Raw-insert a tasks row owned by ``prd_id`` (matches
    tests/test_claims.py::_insert_task_raw)."""
    conn.execute(
        """INSERT INTO tasks
        (id, feature_id, title, description, status, priority,
         dependencies, conflict_groups, scores, acceptance_criteria,
         implementation_notes, verification, likely_files,
         prd_id, created_at, updated_at)
        VALUES (?, 'F001', ?, 'desc', ?, 'medium', '[]', '[]', '{}', '[]',
                '[]', '{}', '[]', ?, ?, ?)""",
        (task_id, f"Task {task_id}", status, prd_id,
         _T0.isoformat(), _T0.isoformat()),
    )
    conn.commit()


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_PRD_ENV, raising=False)


# ---------------------------------------------------------------------------
# CLI resolver — precedence tiers
# ---------------------------------------------------------------------------


def test_explicit_prd_wins_and_is_stripped(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An explicit --prd value beats $ANVIL_PRD and the DB, and is stripped."""
    monkeypatch.setenv(_PRD_ENV, "v0.2")
    b = _make_backend(tmp_path)
    try:
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        try:
            _insert_prd_raw(conn, prd_id="default", is_default=1)
            _insert_prd_raw(conn, prd_id="v0.2")
        finally:
            conn.close()
        assert resolve_prd_id(b, "v0.3") == "v0.3"
        assert resolve_prd_id(b, "  spaced  ") == "spaced"
    finally:
        b.close()


def test_anvil_prd_env_beats_db_tiers(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """$ANVIL_PRD is consulted when no explicit id is passed, beating the DB."""
    monkeypatch.setenv(_PRD_ENV, "from-env")
    b = _make_backend(tmp_path)
    try:
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        try:
            _insert_prd_raw(conn, prd_id="default", is_default=1)
        finally:
            conn.close()
        # explicit None / blank falls through to the env tier
        assert resolve_prd_id(b, None) == "from-env"
        assert resolve_prd_id(b, "") == "from-env"
        assert resolve_prd_id(b, "   ") == "from-env"
    finally:
        b.close()


def test_single_prd_resolves_without_env(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Exactly one PRD resolves to it even when it is not marked default."""
    _clear_env(monkeypatch)
    b = _make_backend(tmp_path)
    try:
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        try:
            _insert_prd_raw(conn, prd_id="only-one", is_default=0)
        finally:
            conn.close()
        assert resolve_prd_id(b, None) == "only-one"
    finally:
        b.close()


def test_default_prd_resolves_when_multiple(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Several PRDs with one marked default resolve to the default id."""
    _clear_env(monkeypatch)
    b = _make_backend(tmp_path)
    try:
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        try:
            _insert_prd_raw(conn, prd_id="default", is_default=1)
            _insert_prd_raw(conn, prd_id="v0.2", is_default=0)
        finally:
            conn.close()
        assert resolve_prd_id(b, None) == "default"
    finally:
        b.close()


def test_ambiguity_raises_when_multiple_no_default(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Several non-default PRDs, no env, no explicit id -> ambiguity error."""
    _clear_env(monkeypatch)
    b = _make_backend(tmp_path)
    try:
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        try:
            _insert_prd_raw(conn, prd_id="v0.1", is_default=0)
            _insert_prd_raw(conn, prd_id="v0.2", is_default=0)
        finally:
            conn.close()
        with pytest.raises(PrdAmbiguityError) as exc:
            resolve_prd_id(b, None)
        # The error names the available PRDs and the override knobs.
        assert "v0.1" in exc.value.message
        assert "v0.2" in exc.value.message
        assert _PRD_ENV in exc.value.message
    finally:
        b.close()


def test_zero_prd_raises_with_create_prd_message(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """No PRD, no env, no explicit id -> raises ('missing' branch of T018), and
    the message tells the user to PARSE a PRD rather than falsely claiming
    'multiple PRDs exist' / listing '(none)'."""
    _clear_env(monkeypatch)
    b = _make_backend(tmp_path)  # freshly-initialized DB: list_prds() == []
    try:
        assert b.list_prds() == []
        with pytest.raises(PrdAmbiguityError) as exc:
            resolve_prd_id(b, None)
        msg = exc.value.message
        # The zero-PRD message must NOT misdescribe the empty case.
        assert "Multiple PRDs" not in msg
        assert "(none)" not in msg
        # It points at the real remediation (create/parse a PRD) and the knobs.
        assert "PRD" in msg
        assert _PRD_ENV in msg
    finally:
        b.close()


# ---------------------------------------------------------------------------
# PRD_OPTION wiring
# ---------------------------------------------------------------------------


def test_prd_option_wires_flag_and_envvar() -> None:
    """PRD_OPTION is the shared --prd / ANVIL_PRD Typer option (default None)."""
    info = PRD_OPTION
    assert info.default is None
    assert "--prd" in info.param_decls
    assert info.envvar == _PRD_ENV


# ---------------------------------------------------------------------------
# CLI <-> MCP parity — identical ids for identical DB + env inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prds", "env", "explicit", "expected"),
    [
        # explicit wins over everything
        ([("default", 1), ("v0.2", 0)], "from-env", "v9", "v9"),
        # env beats the DB
        ([("default", 1)], "from-env", None, "from-env"),
        # single PRD, no env/explicit
        ([("solo", 0)], None, None, "solo"),
        # multiple PRDs with a default
        ([("default", 1), ("v0.2", 0), ("v0.3", 0)], None, None, "default"),
    ],
)
def test_cli_and_mcp_resolvers_agree(  # type: ignore[no-untyped-def]
    monkeypatch, tmp_path, prds, env, explicit, expected
) -> None:
    """resolve_prd_id (CLI) and _resolve_prd_id (MCP) return identical ids for
    identical DB + env inputs across the precedence tiers."""
    if env is None:
        _clear_env(monkeypatch)
    else:
        monkeypatch.setenv(_PRD_ENV, env)

    b = _make_backend(tmp_path)
    try:
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        try:
            for prd_id, is_default in prds:
                _insert_prd_raw(conn, prd_id=prd_id, is_default=is_default)
        finally:
            conn.close()

        cli_id = resolve_prd_id(b, explicit)
        mcp_id = mcp_resolve_prd_id(b, explicit)
        assert cli_id == mcp_id == expected
    finally:
        b.close()


def test_cli_and_mcp_agree_on_ambiguity(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Both resolvers reject the same ambiguous DB — the CLI with a
    ClickException, the MCP with a ToolError carrying the same message."""
    from fastmcp.exceptions import ToolError

    _clear_env(monkeypatch)
    b = _make_backend(tmp_path)
    try:
        conn = sqlite3.connect(str(tmp_path / "state.db"))
        try:
            _insert_prd_raw(conn, prd_id="v0.1", is_default=0)
            _insert_prd_raw(conn, prd_id="v0.2", is_default=0)
        finally:
            conn.close()

        with pytest.raises(PrdAmbiguityError) as cli_exc:
            resolve_prd_id(b, None)
        with pytest.raises(ToolError) as mcp_exc:
            mcp_resolve_prd_id(b, None)
        assert cli_exc.value.message == str(mcp_exc.value)
    finally:
        b.close()


# ---------------------------------------------------------------------------
# AC4 — read-only rollups never resolve a single PRD / never raise ambiguity
# ---------------------------------------------------------------------------


def test_get_project_status_never_raises_on_ambiguous_db(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The read-only rollup ``get_project_status`` aggregates across ALL PRDs and
    must never resolve a single PRD — so it stays clean on a DB that is
    ambiguous for the mutating resolvers (several PRDs, no default).

    Discriminating (AC4): the DB carries tasks in BOTH non-default PRDs, so the
    assertions catch a regression that wires the rollup to a single-PRD resolver
    or sums only the default PRD's tasks — either would drop the foreign-PRD
    task and fail ``total_tasks``/``ready_queue_depth``/``task_counts``."""
    from anvil.mcp_server import get_project_status

    _clear_env(monkeypatch)
    # The autouse local-layout fixture maps cwd -> <cwd>/.anvil, so build the
    # project state under tmp_path/.anvil and point the rollup at tmp_path.
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    b = _make_backend(state_dir)
    try:
        conn = sqlite3.connect(str(state_dir / "state.db"))
        try:
            # Same ambiguous shape that makes resolve_prd_id raise.
            _insert_prd_raw(conn, prd_id="v0.1", is_default=0)
            _insert_prd_raw(conn, prd_id="v0.2", is_default=0)
            # One ready + one claimed task in EACH PRD, so a single-PRD or
            # default-only aggregation would miss half of them.
            _insert_feature_raw(conn)
            _insert_task_raw(conn, task_id="T100", status="ready", prd_id="v0.1")
            _insert_task_raw(conn, task_id="T101", status="claimed", prd_id="v0.1")
            _insert_task_raw(conn, task_id="T200", status="ready", prd_id="v0.2")
            _insert_task_raw(conn, task_id="T201", status="claimed", prd_id="v0.2")
        finally:
            conn.close()
        # Sanity: this DB IS ambiguous for the mutating resolver.
        with pytest.raises(PrdAmbiguityError):
            resolve_prd_id(b, None)
    finally:
        b.close()

    # The rollup, pointed at the same project, returns without raising AND
    # aggregates across BOTH ambiguous PRDs (never resolving a single one).
    resp = get_project_status(cwd=str(tmp_path))
    assert resp.initialized is True
    assert resp.total_tasks == 4
    assert resp.ready_queue_depth == 2
    assert resp.task_counts.ready == 2
    assert resp.task_counts.claimed == 2
