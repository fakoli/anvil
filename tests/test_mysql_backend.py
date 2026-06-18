"""MySQL backend tests — all skip-gated on MYSQL_TEST_URL + PyMySQL.

The single most important test here is ``test_mysql_single_winner_across_processes``
(spec §0/§4.2): it spawns the contenders as separate OS PROCESSES, each opening
its OWN MySQLBackend connection against the shared server, all racing the same
task. The SQLite single-winner guarantee leans on a host-local ``flock`` that is
meaningless across hosts; this test proves the MySQL path relocated the guarantee
INTO THE DATABASE (the ``uq_one_active_claim_per_task`` UNIQUE + ``FOR UPDATE``
row locks), since separate processes are exactly the cross-host shape the
multi-thread contract test cannot detect.

Run with, e.g.::

    docker run --rm -d -e MYSQL_ALLOW_EMPTY_PASSWORD=1 -e MYSQL_DATABASE=anvil \\
        -p 3306:3306 mysql:8
    MYSQL_TEST_URL="mysql://root@127.0.0.1:3306/anvil" \\
        uv run pytest ../tests/test_mysql_backend.py -q

When MYSQL_TEST_URL is unset (or PyMySQL is not installed) every test here is
SKIPPED, never failed — so the default suite stays green on a laptop with no
MySQL.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from anvil.clock import SystemClock
from anvil.state.models import EventDraft

_MYSQL_URL = os.environ.get("MYSQL_TEST_URL")

pytestmark = pytest.mark.skipif(
    not _MYSQL_URL,
    reason="set MYSQL_TEST_URL to run the MySQL backend tests",
)

_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drop_all_tables(dsn: str) -> None:
    import pymysql

    from anvil.state.mysql import parse_mysql_dsn

    conn = pymysql.connect(**parse_mysql_dsn(dsn))
    try:
        cur = conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        cur.execute("SHOW TABLES")
        for (table,) in cur.fetchall():
            cur.execute(f"DROP TABLE IF EXISTS `{table}`")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
    finally:
        conn.close()


def _make_backend(dsn: str, tmp_path: Path) -> Any:
    from anvil.state.mysql import MySQLBackend

    events_path = str(tmp_path / "events.jsonl")
    Path(events_path).touch()
    b = MySQLBackend(dsn=dsn, events_path=events_path, clock=SystemClock())
    b.initialize()
    return b


def _setup_project_prd_feature(b: Any) -> None:
    """Stand up a reviewed PRD + feature so the claim PRD-gate passes."""
    b.append(
        EventDraft(
            timestamp=_T0,
            actor="test",
            action="project.created",
            target_kind="project",
            target_id="proj-1",
            payload_json={
                "id": "proj-1",
                "name": "Test Project",
                "description": "",
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
            },
        )
    )
    b.append(
        EventDraft(
            timestamp=_T0,
            actor="test",
            action="state.initialized",
            target_kind="project",
            target_id="proj-1",
            payload_json={},
        )
    )
    b.append(
        EventDraft(
            timestamp=_T0,
            actor="test",
            action="prd.parsed",
            target_kind="prd",
            target_id="proj-1",
            payload_json={
                "project_id": "proj-1",
                "status": "draft",
                "summary": "Test PRD.",
                "goals": ["Goal one."],
                "non_goals": [],
                "requirements": [
                    {
                        "id": "R001",
                        "prd_section": "requirements",
                        "text": "Req 1.",
                        "source_paragraph": None,
                        "derived": False,
                    }
                ],
                "acceptance_criteria": ["AC one."],
                "risks": [],
                "open_questions": [],
            },
        )
    )
    b.append(
        EventDraft(
            timestamp=_T0,
            actor="test",
            action="prd.reviewed",
            target_kind="prd",
            target_id="proj-1",
            payload_json={"project_id": "proj-1", "reviewer": "alice"},
        )
    )
    b.append(
        EventDraft(
            timestamp=_T0,
            actor="test",
            action="feature.created",
            target_kind="feature",
            target_id="F001",
            payload_json={
                "id": "F001",
                "title": "Feature F001",
                "description": "desc",
                "status": "proposed",
                "requirements": [],
                "tasks": [],
            },
        )
    )


def _add_ready_task(b: Any, task_id: str, files: list[str]) -> None:
    b.append(
        EventDraft(
            timestamp=_T0,
            actor="test",
            action="task.created",
            target_kind="task",
            target_id=task_id,
            payload_json={
                "id": task_id,
                "feature_id": "F001",
                "title": f"Task {task_id}",
                "description": "desc",
                "status": "proposed",
                "priority": "medium",
                "dependencies": [],
                "conflict_groups": [],
                "scores": {},
                "acceptance_criteria": [],
                "implementation_notes": [],
                "verification": {},
                "likely_files": files,
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
            },
        )
    )
    b.append(
        EventDraft(
            timestamp=_T0,
            actor="test",
            action="task.status_changed",
            target_kind="task",
            target_id=task_id,
            payload_json={"task_id": task_id, "from": "proposed", "to": "ready"},
        )
    )


# ---------------------------------------------------------------------------
# Cross-process single-winner — the spec §0 guard.
# ---------------------------------------------------------------------------


def _claim_worker(dsn: str, events_dir: str, actor: str, ready_evt, start_evt, result_q) -> None:  # type: ignore[no-untyped-def]
    """Run in a SEPARATE PROCESS: open an own backend, claim the same task.

    Each process opens its OWN MySQLBackend (its own connection), so this is the
    genuine cross-host shape — no shared in-process lock can help. Reports
    ("won"|"lost"|"dirty:<repr>") via the result queue.
    """
    from anvil.claims.manager import ClaimError, ClaimManager
    from anvil.clock import SystemClock as _Clock
    from anvil.state.mysql import MySQLBackend as _Backend

    events_path = os.path.join(events_dir, f"events-{actor}.jsonl")
    open(events_path, "a").close()
    backend = _Backend(dsn=dsn, events_path=events_path, clock=_Clock())
    backend.initialize()
    try:
        manager = ClaimManager(backend, _Clock(), actor=actor)
        ready_evt.set()
        start_evt.wait()
        try:
            manager.claim("T001", expected_files=["a.py"])
            result_q.put((actor, "won"))
        except ClaimError:
            result_q.put((actor, "lost"))
        except BaseException as exc:  # noqa: BLE001 — surface any leak
            result_q.put((actor, f"dirty:{exc!r}"))
    finally:
        backend.close()


def test_mysql_single_winner_across_processes(tmp_path: Path) -> None:
    """N separate PROCESSES race the same task — exactly one wins.

    This is the test the flock-based SQLite path cannot satisfy cross-host and
    the UNIQUE-constraint MySQL path must (spec §0/§4.2). Each process has its
    own connection; the only thing that can make exactly one win is the database
    itself (FOR UPDATE row locks + the uq_one_active_claim_per_task UNIQUE).
    """
    assert _MYSQL_URL is not None
    pytest.importorskip("pymysql")
    _drop_all_tables(_MYSQL_URL)

    # Set up state once in the parent.
    b = _make_backend(_MYSQL_URL, tmp_path)
    try:
        _setup_project_prd_feature(b)
        _add_ready_task(b, "T001", ["a.py"])
    finally:
        b.close()

    n_procs = 12
    # "spawn" so each child re-imports cleanly (macOS default; explicit for CI).
    ctx = mp.get_context("spawn")
    mgr = ctx.Manager()
    result_q = mgr.Queue()
    ready_evts = [ctx.Event() for _ in range(n_procs)]
    start_evt = ctx.Event()
    procs = [
        ctx.Process(
            target=_claim_worker,
            args=(
                _MYSQL_URL,
                str(tmp_path),
                f"agent-{i}",
                ready_evts[i],
                start_evt,
                result_q,
            ),
        )
        for i in range(n_procs)
    ]
    for p in procs:
        p.start()
    # Wait until every child is connected and parked at the barrier, then
    # release them all at once for the tightest possible race.
    for ev in ready_evts:
        assert ev.wait(timeout=60), "a claim worker failed to become ready"
    start_evt.set()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"a claim worker crashed (exitcode {p.exitcode})"

    results = [result_q.get() for _ in range(n_procs)]
    dirty = [r for r in results if r[1].startswith("dirty:")]
    assert not dirty, f"losers must fail cleanly, got dirty outcomes: {dirty}"
    winners = [r for r in results if r[1] == "won"]
    assert len(winners) == 1, (
        f"cross-process race produced {len(winners)} winners (expected exactly "
        f"1 — the DB, not a host-local lock, must enforce single-winner): "
        f"{results}"
    )

    # The DB itself agrees: exactly one ACTIVE claim for the task.
    b = _make_backend(_MYSQL_URL, tmp_path)
    try:
        active = [c for c in b.list_active_claims() if c.task_id == "T001"]
        assert len(active) == 1, (
            f"DB shows {len(active)} active claims for T001 (expected 1)"
        )
    finally:
        b.close()


def test_mysql_single_winner_across_processes_overlapping_files(tmp_path: Path) -> None:
    """Two DIFFERENT tasks sharing a file, raced across processes — one winner.

    The cross-task file-overlap shape (no per-task UNIQUE backstop applies) must
    also serialize to a single winner across separate processes.
    """
    assert _MYSQL_URL is not None
    pytest.importorskip("pymysql")
    _drop_all_tables(_MYSQL_URL)

    b = _make_backend(_MYSQL_URL, tmp_path)
    try:
        _setup_project_prd_feature(b)
        _add_ready_task(b, "T001", ["shared.py", "one.py"])
        _add_ready_task(b, "T002", ["shared.py", "two.py"])
    finally:
        b.close()

    n_procs = 8
    ctx = mp.get_context("spawn")
    mgr = ctx.Manager()
    result_q = mgr.Queue()
    ready_evts = [ctx.Event() for _ in range(n_procs)]
    start_evt = ctx.Event()
    procs = [
        ctx.Process(
            target=_claim_worker_files,
            args=(
                _MYSQL_URL,
                str(tmp_path),
                f"agent-{i}",
                "T001" if i % 2 == 0 else "T002",
                ["shared.py", "one.py"] if i % 2 == 0 else ["shared.py", "two.py"],
                ready_evts[i],
                start_evt,
                result_q,
            ),
        )
        for i in range(n_procs)
    ]
    for p in procs:
        p.start()
    for ev in ready_evts:
        assert ev.wait(timeout=60), "a claim worker failed to become ready"
    start_evt.set()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"a claim worker crashed (exitcode {p.exitcode})"

    results = [result_q.get() for _ in range(n_procs)]
    dirty = [r for r in results if r[1].startswith("dirty:")]
    assert not dirty, f"losers must fail cleanly, got: {dirty}"
    winners = [r for r in results if r[1] == "won"]
    assert len(winners) == 1, (
        f"cross-process file-overlap race produced {len(winners)} winners "
        f"(expected exactly 1): {results}"
    )

    # No two active claims share a file.
    b = _make_backend(_MYSQL_URL, tmp_path)
    try:
        seen: set[str] = set()
        for c in b.list_active_claims():
            clash = seen & set(c.expected_files)
            assert not clash, f"two active claims share files {sorted(clash)}"
            seen.update(c.expected_files)
    finally:
        b.close()


def _claim_worker_files(dsn, events_dir, actor, task_id, files, ready_evt, start_evt, result_q) -> None:  # type: ignore[no-untyped-def]
    from anvil.claims.manager import ClaimError, ClaimManager
    from anvil.clock import SystemClock as _Clock
    from anvil.state.mysql import MySQLBackend as _Backend

    events_path = os.path.join(events_dir, f"events-{actor}.jsonl")
    open(events_path, "a").close()
    backend = _Backend(dsn=dsn, events_path=events_path, clock=_Clock())
    backend.initialize()
    try:
        manager = ClaimManager(backend, _Clock(), actor=actor)
        ready_evt.set()
        start_evt.wait()
        try:
            manager.claim(task_id, expected_files=files)
            result_q.put((actor, "won"))
        except ClaimError:
            result_q.put((actor, "lost"))
        except BaseException as exc:  # noqa: BLE001
            result_q.put((actor, f"dirty:{exc!r}"))
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Schema / lifecycle unit tests.
# ---------------------------------------------------------------------------


def test_initialize_stamps_schema_version(tmp_path: Path) -> None:
    from anvil.state.schema import SCHEMA_VERSION

    assert _MYSQL_URL is not None
    pytest.importorskip("pymysql")
    _drop_all_tables(_MYSQL_URL)
    b = _make_backend(_MYSQL_URL, tmp_path)
    try:
        assert b.get_schema_version() == SCHEMA_VERSION
    finally:
        b.close()


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    from anvil.state.schema import SCHEMA_VERSION

    assert _MYSQL_URL is not None
    pytest.importorskip("pymysql")
    _drop_all_tables(_MYSQL_URL)
    b = _make_backend(_MYSQL_URL, tmp_path)
    try:
        b.initialize()  # second call — must not raise, version unchanged
        b.initialize()
        assert b.get_schema_version() == SCHEMA_VERSION
    finally:
        b.close()


def test_future_schema_version_raises(tmp_path: Path) -> None:
    import pymysql

    from anvil.state.backend import SchemaMismatch
    from anvil.state.mysql import parse_mysql_dsn
    from anvil.state.schema import SCHEMA_VERSION

    assert _MYSQL_URL is not None
    pytest.importorskip("pymysql")
    _drop_all_tables(_MYSQL_URL)
    b = _make_backend(_MYSQL_URL, tmp_path)
    b.close()
    # Bump the stamped version above SCHEMA_VERSION → reopen must SchemaMismatch.
    conn = pymysql.connect(**parse_mysql_dsn(_MYSQL_URL))
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM schema_version")
        cur.execute(
            "INSERT INTO schema_version (version) VALUES (%s)",
            (SCHEMA_VERSION + 1,),
        )
        conn.commit()
    finally:
        conn.close()
    from anvil.state.mysql import MySQLBackend

    events_path = str(tmp_path / "events2.jsonl")
    Path(events_path).touch()
    backend = MySQLBackend(
        dsn=_MYSQL_URL, events_path=events_path, clock=SystemClock()
    )
    with pytest.raises(SchemaMismatch):
        backend.initialize()
    backend.close()


# ---------------------------------------------------------------------------
# The UNIQUE backstop directly (FOR UPDATE path bypassed).
# ---------------------------------------------------------------------------


def test_unique_active_claim_constraint_rejects_second(tmp_path: Path) -> None:
    """Inserting a SECOND active claim for a task directly raises IntegrityError.

    Proves the uq_one_active_claim_per_task generated-column UNIQUE is the
    host-independent backstop — correctness no longer depends on any app lock.
    """
    import pymysql

    from anvil.state.mysql import parse_mysql_dsn

    assert _MYSQL_URL is not None
    pytest.importorskip("pymysql")
    _drop_all_tables(_MYSQL_URL)
    b = _make_backend(_MYSQL_URL, tmp_path)
    try:
        _setup_project_prd_feature(b)
        _add_ready_task(b, "T001", ["a.py"])
    finally:
        b.close()

    conn = pymysql.connect(**parse_mysql_dsn(_MYSQL_URL))
    try:
        cur = conn.cursor()

        def _insert(claim_id: str) -> None:
            cur.execute(
                """INSERT INTO claims
                (id, task_id, claimed_by, claim_type, status, branch,
                 worktree_path, expected_files, created_at, lease_expires_at,
                 last_heartbeat_at)
                VALUES (%s, 'T001', 'a', 'task', 'active', NULL, NULL, '[]',
                        %s, %s, %s)""",
                (claim_id, _T0.isoformat(), _T0.isoformat(), _T0.isoformat()),
            )

        _insert("C000001")
        conn.commit()
        with pytest.raises(pymysql.err.IntegrityError):
            _insert("C000002")
            conn.commit()
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# replay_from_empty equivalence.
# ---------------------------------------------------------------------------


def test_replay_from_empty_rebuilds_equivalent_state(tmp_path: Path) -> None:
    """A MySQL DB rebuilt from events.jsonl row-matches one built by append()."""
    assert _MYSQL_URL is not None
    pytest.importorskip("pymysql")
    _drop_all_tables(_MYSQL_URL)

    events_path = str(tmp_path / "events.jsonl")
    Path(events_path).touch()
    from anvil.state.mysql import MySQLBackend

    b = MySQLBackend(dsn=_MYSQL_URL, events_path=events_path, clock=SystemClock())
    b.initialize()
    try:
        _setup_project_prd_feature(b)
        _add_ready_task(b, "T001", ["a.py"])
        _add_ready_task(b, "T002", ["b.py"])
        live_tasks = {t.id: t.model_dump(mode="json") for t in b.list_tasks()}
        live_project = b.get_project()
        live_prd = b.get_prd()
    finally:
        b.close()

    # Rebuild from the JSONL shadow into the same (now-truncated) DB.
    b2 = MySQLBackend(dsn=_MYSQL_URL, events_path=events_path, clock=SystemClock())
    b2.initialize()
    try:
        b2.replay_from_empty(events_path)
        replayed_tasks = {t.id: t.model_dump(mode="json") for t in b2.list_tasks()}
        assert replayed_tasks == live_tasks
        assert b2.get_project() == live_project
        assert b2.get_prd() == live_prd
    finally:
        b2.close()


# ---------------------------------------------------------------------------
# DSN parsing.
# ---------------------------------------------------------------------------


def test_dsn_parse_basic() -> None:
    from anvil.state.mysql import parse_mysql_dsn

    kw = parse_mysql_dsn("mysql://anvil:secret@db.internal:3307/anvil_state")
    assert kw["host"] == "db.internal"
    assert kw["port"] == 3307
    assert kw["user"] == "anvil"
    assert kw["password"] == "secret"
    assert kw["database"] == "anvil_state"
    assert kw["charset"] == "utf8mb4"
    assert kw["autocommit"] is True


def test_dsn_default_port() -> None:
    from anvil.state.mysql import parse_mysql_dsn

    kw = parse_mysql_dsn("mysql://root@localhost/anvil")
    assert kw["port"] == 3306


def test_dsn_password_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from anvil.state.mysql import parse_mysql_dsn

    monkeypatch.setenv("ANVIL_MYSQL_PASSWORD", "from-env")
    kw = parse_mysql_dsn("mysql://anvil@db/anvil_state")
    assert kw["password"] == "from-env"


def test_dsn_ssl_ca_threads_into_ssl() -> None:
    from anvil.state.mysql import parse_mysql_dsn

    kw = parse_mysql_dsn("mysql://u@h/db?ssl_ca=/etc/ssl/rds.pem")
    assert kw["ssl"] == {"ca": "/etc/ssl/rds.pem"}


def test_dsn_missing_database_raises() -> None:
    from anvil.state.mysql import parse_mysql_dsn

    with pytest.raises(ValueError, match="no database name"):
        parse_mysql_dsn("mysql://root@localhost")


def test_dsn_wrong_scheme_raises() -> None:
    from anvil.state.mysql import parse_mysql_dsn

    with pytest.raises(ValueError, match="scheme"):
        parse_mysql_dsn("postgres://root@localhost/anvil")


def test_dsn_blank_raises() -> None:
    from anvil.state.mysql import parse_mysql_dsn

    with pytest.raises(ValueError, match="blank"):
        parse_mysql_dsn("")


# ---------------------------------------------------------------------------
# Aurora "same driver" proof — no endpoint-shape branching exists.
# ---------------------------------------------------------------------------


def test_aurora_uses_identical_code_path() -> None:
    """Pointing the DSN at an Aurora-shaped writer endpoint parses identically.

    There is no Aurora-specific branch anywhere in the backend — Aurora MySQL is
    selected purely by the DSN host. This asserts the parser treats an Aurora
    cluster endpoint exactly like any other MySQL host.
    """
    from anvil.state.mysql import parse_mysql_dsn

    aurora = (
        "mysql://anvil:secret@my-cluster.cluster-abc.us-east-1.rds.amazonaws.com"
        ":3306/anvil_state?ssl_ca=/etc/ssl/rds-combined-ca-bundle.pem"
    )
    plain = "mysql://anvil:secret@db.internal:3306/anvil_state"
    a = parse_mysql_dsn(aurora)
    p = parse_mysql_dsn(plain)
    # Same keys, same handling — only host/ssl differ by input, nothing else.
    assert set(a) >= set(p)
    assert a["port"] == p["port"] == 3306
    assert a["database"] == p["database"] == "anvil_state"
