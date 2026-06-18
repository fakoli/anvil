"""MySQL/Aurora backend implementing the Backend protocol.

Sibling of :class:`anvil.state.sqlite.SqliteBackend`. It REUSES every
dialect-neutral piece of the SQLite impl — the ``_check_*`` / ``_write_*``
decide/apply split, the ``ActionSpec`` dispatch table, the query helpers, and
the row → model converters — by subclassing ``SqliteBackend`` and overriding
only the seven infrastructure seams enumerated in
``docs/specs/2026-06-18-mysql-backend.md`` §2:

  1. connection lifecycle (PyMySQL instead of sqlite3, no WAL);
  2. claim check-and-write atomicity (InnoDB ``SELECT ... FOR UPDATE`` row locks
     + the ``uq_one_active_claim_per_task`` UNIQUE backstop, replacing the
     host-local flock that the SQLite single-winner guarantee leans on);
  3. event-id allocation (DB ``AUTO_INCREMENT`` allocator, not the per-host
     ``events.jsonl`` counter);
  4. ``schema_version`` table instead of ``PRAGMA user_version``;
  5. MySQL DDL (``schema.generate_schema_sql_mysql``);
  6. no WAL — InnoDB redo log + MVCC are the always-on equivalent;
  7. connection/lifecycle/replay/error mapping per the Protocol contract.

The ONE load-bearing correctness move (spec §0): on a shared MySQL/Aurora
server reached by N agents on N machines, ``flock`` on a local file is
meaningless. So the single-winner guarantee is RELOCATED INTO THE DATABASE —
the ``uq_one_active_claim_per_task`` UNIQUE on a ``status='active'``-generated
column is the engine-enforced, host-independent backstop, and the
``SELECT ... FOR UPDATE`` on the contended task row is the clean serialization
+ readable-rejection path. This backend therefore does NOT use ``flock`` and
does NOT rely on the in-process ``threading.Lock`` for cross-host correctness
(it keeps a per-instance lock only because a single PyMySQL connection is not
thread-safe within one process).

How the inherited code is reused unchanged: a tiny connection adapter
(``_MySQLConnAdapter``) makes a PyMySQL connection quack like a
``sqlite3.Connection`` — it translates ``?`` / ``:name`` placeholders to
PyMySQL's ``%s`` / ``%(name)s``, rewrites the handful of SQLite-only INSERT
idioms (``INSERT OR IGNORE``, ``INSERT OR REPLACE``,
``INSERT ... ON CONFLICT(...) DO UPDATE``) into their MySQL equivalents, makes
``BEGIN IMMEDIATE`` a plain ``START TRANSACTION``, no-ops ``PRAGMA``, and
returns hybrid rows that support BOTH ``dict(row)`` and ``row[i]`` (the
SQLite row-converters use both). PyMySQL is imported lazily so a default
(SQLite) install never imports it.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from anvil.state.backend import (
    EventRejected,
    SchemaMismatch,
    StateLocked,
    TransactionAborted,
)
from anvil.state.schema import DDL_MYSQL, SCHEMA_VERSION
from anvil.state.sqlite import SqliteBackend

if TYPE_CHECKING:
    from anvil.clock import Clock

logger = logging.getLogger(__name__)

# InnoDB error numbers we translate to the Protocol's exception surface.
_ERR_DUP_ENTRY = 1062  # UNIQUE / PK violation → EventRejected (claim backstop)
_ERR_LOCK_WAIT_TIMEOUT = 1205  # innodb_lock_wait_timeout → StateLocked
_ERR_DEADLOCK = 1213  # deadlock victim → retried, then StateLocked
_ERR_DUP_COLUMN = 1060  # duplicate column on ALTER → tolerated in migration
_GONE_AWAY = (2006, 2013)  # server gone away / lost connection → reconnect

_DEADLOCK_RETRIES = 3
_LOCK_WAIT_TIMEOUT_S = 5


# ---------------------------------------------------------------------------
# DSN parsing
# ---------------------------------------------------------------------------


def parse_mysql_dsn(dsn: str) -> dict[str, Any]:
    """Parse a SQLAlchemy-style MySQL URL into ``pymysql.connect`` kwargs.

    Standard URL form (spec §1)::

        mysql://anvil:secret@db.internal:3306/anvil?charset=utf8mb4&ssl_ca=/p.pem

    Parsed with the stdlib ``urllib.parse`` (no SQLAlchemy dependency — we only
    read the parts and hand them to PyMySQL). The password may be omitted from
    the URL and supplied via the ``ANVIL_MYSQL_PASSWORD`` env var so secrets are
    not baked into committed config. Recognised query params: ``charset``
    (default ``utf8mb4``), ``ssl_ca`` (threaded into PyMySQL's
    ``ssl={"ca": ...}`` for Aurora/RDS TLS), and ``connect_timeout``.

    Aurora MySQL needs ZERO new code here: it is wire-compatible with MySQL
    8.0, so pointing the DSN at the cluster *writer* endpoint is all that is
    required. (Writes MUST go to the writer endpoint — a reader endpoint's
    replica lag would let two agents read a stale "no active claim" state and
    both win, which is spec §0 in a different costume.)

    Raises:
        ValueError: blank DSN, wrong scheme, or missing database name.
    """
    if not dsn or not dsn.strip():
        raise ValueError(
            "MySQL DSN is blank. Set `mysql_dsn` in config.yaml to a "
            "mysql://user[:pass]@host[:port]/database URL."
        )
    parsed = urlparse(dsn.strip())
    if parsed.scheme not in ("mysql", "mysql+pymysql"):
        raise ValueError(
            f"MySQL DSN scheme must be 'mysql', got {parsed.scheme!r} in {dsn!r}."
        )
    database = parsed.path.lstrip("/")
    if not database:
        raise ValueError(
            f"MySQL DSN {dsn!r} has no database name "
            "(expected mysql://host/<database>)."
        )

    password = parsed.password
    if password is None:
        password = os.environ.get("ANVIL_MYSQL_PASSWORD")

    query = parse_qs(parsed.query)

    def _q(key: str) -> str | None:
        vals = query.get(key)
        return vals[0] if vals else None

    kwargs: dict[str, Any] = {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": parsed.username or "root",
        "database": database,
        "charset": _q("charset") or "utf8mb4",
        "autocommit": True,
    }
    if password is not None:
        kwargs["password"] = password
    ssl_ca = _q("ssl_ca")
    if ssl_ca:
        kwargs["ssl"] = {"ca": ssl_ca}
    connect_timeout = _q("connect_timeout")
    if connect_timeout:
        kwargs["connect_timeout"] = int(connect_timeout)
    return kwargs


# ---------------------------------------------------------------------------
# SQL dialect translation (SQLite source SQL → MySQL)
# ---------------------------------------------------------------------------

# ``INSERT ... ON CONFLICT(cols) DO UPDATE SET a = excluded.a, ...`` →
# ``INSERT ... ON DUPLICATE KEY UPDATE a = VALUES(a), ...``. The capture is the
# SET-list, with SQL line comments stripped (the tasks upsert carries a
# multi-line ``-- status is intentionally OMITTED`` comment inside the SET).
_ON_CONFLICT_RE = re.compile(
    r"ON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET\s+(.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _translate_sql(sql: str) -> str:
    """Rewrite the SQLite-dialect ``sql`` string into its MySQL equivalent.

    Mechanical and self-contained (spec §2 "the one concession"):

    - ``BEGIN IMMEDIATE`` / ``BEGIN`` → ``START TRANSACTION`` (InnoDB; the
      explicit ``FOR UPDATE`` row locks that replace SQLite's coarse db-level
      write lock are added at the call sites that need them, not here).
    - ``INSERT OR IGNORE`` → ``INSERT IGNORE``.
    - ``INSERT OR REPLACE`` → ``REPLACE``.
    - ``INSERT ... ON CONFLICT(...) DO UPDATE SET x = excluded.x`` →
      ``INSERT ... ON DUPLICATE KEY UPDATE x = VALUES(x)``.
    - ``CAST(SUBSTR(id, 2) AS INTEGER)`` → ``CAST(SUBSTRING(id, 2) AS UNSIGNED)``
      (the ``_table_max_id`` helper).
    - placeholder translation is done separately, AFTER this rewrite, by
      :func:`_translate_params` because it must also reshape the params.
    """
    s = sql

    # ON CONFLICT ... DO UPDATE → ON DUPLICATE KEY UPDATE (do this before the
    # INSERT-idiom swaps so the regex sees the original keyword spelling).
    m = _ON_CONFLICT_RE.search(s)
    if m:
        set_list_raw = m.group(1)
        # Strip SQL line comments that may appear inside the SET list.
        set_lines = [
            ln.split("--", 1)[0] for ln in set_list_raw.splitlines()
        ]
        set_list = " ".join(part.strip() for part in set_lines if part.strip())
        # excluded.col → VALUES(col)
        set_list = re.sub(
            r"excluded\.([A-Za-z_][A-Za-z0-9_]*)",
            r"VALUES(\1)",
            set_list,
        )
        s = s[: m.start()] + "ON DUPLICATE KEY UPDATE " + set_list

    s = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT IGNORE", s, flags=re.IGNORECASE)
    s = re.sub(r"\bINSERT\s+OR\s+REPLACE\b", "REPLACE", s, flags=re.IGNORECASE)
    s = re.sub(r"\bBEGIN\s+IMMEDIATE\b", "START TRANSACTION", s, flags=re.IGNORECASE)
    s = re.sub(r"\bBEGIN\b(?!\s+IMMEDIATE)", "START TRANSACTION", s, flags=re.IGNORECASE)
    s = re.sub(r"\bSUBSTR\b", "SUBSTRING", s, flags=re.IGNORECASE)
    s = re.sub(r"AS\s+INTEGER\b", "AS UNSIGNED", s, flags=re.IGNORECASE)
    # SQLite accepts `RELEASE <savepoint>`; MySQL requires the SAVEPOINT keyword.
    # `SAVEPOINT <name>` and `ROLLBACK TO <name>` are valid in both dialects.
    s = re.sub(
        r"\bRELEASE\s+(?!SAVEPOINT\b)([A-Za-z_][A-Za-z0-9_]*)",
        r"RELEASE SAVEPOINT \1",
        s,
        flags=re.IGNORECASE,
    )
    return s


def _translate_params(sql: str, params: Any) -> tuple[str, Any]:
    """Translate placeholders and reshape ``params`` for PyMySQL.

    sqlite3 accepts ``?`` (positional, with a sequence) and ``:name`` (named,
    with a mapping). PyMySQL's default paramstyle is ``%s`` (positional) and
    ``%(name)s`` (named). A literal ``%`` in the SQL (none today, but defensive)
    is escaped to ``%%`` first so PyMySQL's own formatting does not choke.
    """
    if params is None:
        # No params: still escape stray % so PyMySQL does not try to format.
        return sql.replace("%", "%%"), None

    if isinstance(params, dict):
        out = sql.replace("%", "%%")
        # :name → %(name)s
        out = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", out)
        return out, params

    # Positional: ? → %s
    out = sql.replace("%", "%%").replace("?", "%s")
    return out, tuple(params)


# ---------------------------------------------------------------------------
# Hybrid row — supports BOTH dict(row) and row[i] (the SQLite converters use
# both: _row_to_task/_row_to_claim do dict(row); _row_to_evidence/_requirement
# index positionally).
# ---------------------------------------------------------------------------


class _Row:
    """A query row exposing positional ``row[i]`` AND ``dict(row)`` access."""

    __slots__ = ("_values", "_columns", "_map")

    def __init__(self, columns: tuple[str, ...], values: tuple[Any, ...]) -> None:
        self._columns = columns
        self._values = values
        self._map = dict(zip(columns, values, strict=False))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._map[key]

    def keys(self) -> Any:  # enables dict(row)
        return self._columns

    def __iter__(self) -> Any:  # dict(row) consumes keys(); be tuple-like too
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


# ---------------------------------------------------------------------------
# Connection adapter — quacks like sqlite3.Connection for the inherited code.
# ---------------------------------------------------------------------------


class _MySQLConnAdapter:
    """Quacks like a ``sqlite3.Connection`` over a per-thread PyMySQL connection.

    The inherited ``SqliteBackend`` code calls ``conn.execute(...)`` and then
    ``.fetchone()`` / ``.fetchall()`` on the *connection* (sqlite3 returns a
    cursor from ``Connection.execute``; this adapter returns ``self`` carrying
    the last cursor's rows). Every SQL string is run through ``_translate_sql``
    + ``_translate_params`` so the SQLite-dialect queries execute on MySQL
    unchanged at the call site.

    Thread model: a single PyMySQL connection is NOT thread-safe, and the
    SQLite backend it imitates is opened ``check_same_thread=False`` and shared
    across threads (the MCP server + CLI in one process, and the concurrency
    contract test's many threads). So this adapter holds ONE PyMySQL connection
    PER THREAD, created lazily on first use and tracked so ``close()`` can shut
    them all. Separate connections are exactly what makes InnoDB the
    serialization point: two threads racing the same task each open their own
    connection and contend on the row lock / UNIQUE key in the engine — the
    same shape as two agents on two hosts. The cursor result buffer
    (``_last_rows`` / ``_idx``) is therefore thread-local too, so one thread's
    ``fetchone`` never reads another thread's rows.
    """

    def __init__(self, connect_fn: Any) -> None:
        # connect_fn() -> a freshly opened, session-configured pymysql conn.
        self._connect_fn = connect_fn
        self._tl = threading.local()
        # Registry of every per-thread connection so close() can shut them all.
        self._all_conns: list[Any] = []
        self._registry_lock = threading.Lock()

    def _thread_conn(self) -> Any:
        conn = getattr(self._tl, "conn", None)
        if conn is None:
            conn = self._connect_fn()
            self._tl.conn = conn
            self._tl.last_rows = []
            self._tl.idx = 0
            with self._registry_lock:
                self._all_conns.append(conn)
        return conn

    @property
    def raw(self) -> Any:
        return self._thread_conn()

    def execute(self, sql: str, params: Any = None) -> _MySQLConnAdapter:
        conn = self._thread_conn()
        translated = _translate_sql(sql)
        translated, shaped = _translate_params(translated, params)
        cur = conn.cursor()
        try:
            if shaped is None:
                cur.execute(translated)
            else:
                cur.execute(translated, shaped)
            if cur.description is not None:
                columns = tuple(d[0] for d in cur.description)
                self._tl.last_rows = [_Row(columns, row) for row in cur.fetchall()]
            else:
                self._tl.last_rows = []
        finally:
            cur.close()
        self._tl.idx = 0
        return self

    def fetchone(self) -> _Row | None:
        rows: list[_Row] = getattr(self._tl, "last_rows", [])
        idx: int = getattr(self._tl, "idx", 0)
        if idx >= len(rows):
            return None
        self._tl.idx = idx + 1
        return rows[idx]

    def fetchall(self) -> list[_Row]:
        rows: list[_Row] = getattr(self._tl, "last_rows", [])
        idx: int = getattr(self._tl, "idx", 0)
        self._tl.idx = len(rows)
        return rows[idx:]

    def commit(self) -> None:
        self._thread_conn().commit()

    def rollback(self) -> None:
        self._thread_conn().rollback()

    def discard_thread_connection(self) -> None:
        """Close and forget THIS thread's connection (idempotent).

        For long-lived thread pools and test harnesses that spawn many
        short-lived worker threads: without this the per-thread connections
        accumulate (each thread opens one on first use) and can exhaust the
        server's ``max_connections``. A worker thread calls this before exiting
        to return its connection to the pool.
        """
        conn = getattr(self._tl, "conn", None)
        if conn is None:
            return
        with self._registry_lock:
            try:
                self._all_conns.remove(conn)
            except ValueError:
                pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._tl.conn = None
        self._tl.last_rows = []
        self._tl.idx = 0

    def close(self) -> None:
        with self._registry_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class MySQLBackend(SqliteBackend):
    """MySQL/Aurora backend. Inherits the dialect-neutral logic from SqliteBackend.

    Constructor parameters
    ----------------------
    dsn          : ``mysql://user[:pass]@host[:port]/database[?params]`` URL.
    events_path  : absolute path to the local ``events.jsonl`` audit shadow.
                   For MySQL this is an audit trail only — NOT the id authority
                   and NOT a cross-process serializer (the DB owns both).
    clock        : Clock injected for all timestamp generation.
    durability   : ``"relaxed"`` → ``innodb_flush_log_at_trx_commit = 2``;
                   ``"strict"`` → ``= 1`` (the InnoDB default, per-commit fsync).
    """

    def __init__(
        self,
        *,
        dsn: str | None,
        events_path: str,
        clock: Clock,
        durability: str = "relaxed",
    ) -> None:
        # Reuse the parent's field layout for everything dialect-neutral. We do
        # NOT call super().__init__ because its sqlite-specific defaults (flock
        # sleep/monotonic injectables, _next_seq seeding) are not used here.
        self._dsn = dsn
        self._db_path = "<mysql>"  # parent helpers reference it; never opened.
        self._events_path = events_path
        self._clock = clock
        self._durability = durability
        # MySQL ships at v5 from day one; local mode id format is reused, but
        # ids come from the DB AUTO_INCREMENT, not from events.jsonl.
        self._events_storage = "local"
        self._conn: _MySQLConnAdapter | None = None  # type: ignore[assignment]
        self._next_seq = 0
        self._max_lamport = 0
        # Serializes ONLY the local audit-shadow file append + the in-process
        # id-ordering of the JSONL shadow — NOT the DB transaction. The DB
        # transaction must NOT be wrapped in a process-wide lock, or threads
        # would never contend in InnoDB and the single-winner guarantee would
        # silently revert to a host-local lock (spec §0). Cross-host AND
        # cross-thread single-winner correctness lives in InnoDB row locks +
        # the uq_one_active_claim_per_task UNIQUE.
        self._shadow_lock = threading.Lock()
        self._global_durability_set = False
        self._replaying = False

    # ------------------------------------------------------------------
    # Lifecycle (seam 1, 4, 5, 6, 7)
    # ------------------------------------------------------------------

    def _new_raw_connection(self) -> Any:
        """Open one PyMySQL connection and set its session vars (per-thread)."""
        try:
            import pymysql  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - exercised via message test
            raise TransactionAborted(
                "backend: mysql requires PyMySQL. Install it with "
                "`pip install 'anvil-state[mysql]'`."
            ) from exc
        kwargs = parse_mysql_dsn(self._dsn or "")
        raw = pymysql.connect(**kwargs)
        cur = raw.cursor()
        try:
            # innodb_lock_wait_timeout bounds the FOR UPDATE wait (analog of the
            # flock 5 s budget) — settable per session.
            cur.execute(
                f"SET SESSION innodb_lock_wait_timeout = {_LOCK_WAIT_TIMEOUT_S}"
            )
            # innodb_flush_log_at_trx_commit maps the durability knob, but it is
            # a GLOBAL-only variable in MySQL 8 (errno 1229 on SET SESSION) and
            # setting it GLOBALly needs SYSTEM_VARIABLES_ADMIN. It is a
            # server-wide durability policy, not the single-winner correctness
            # primitive (that is the UNIQUE constraint), so set it once,
            # best-effort, and tolerate a privilege/scope error.
            if not self._global_durability_set:
                flush = 1 if self._durability == "strict" else 2
                try:
                    cur.execute(
                        f"SET GLOBAL innodb_flush_log_at_trx_commit = {flush}"
                    )
                except Exception as exc:  # noqa: BLE001 - best-effort policy
                    logger.debug(
                        "MySQLBackend: could not set "
                        "innodb_flush_log_at_trx_commit=%d (needs SET GLOBAL + "
                        "SYSTEM_VARIABLES_ADMIN); using server default: %s",
                        flush,
                        exc,
                    )
                self._global_durability_set = True
        finally:
            cur.close()
        return raw

    def _connect(self) -> _MySQLConnAdapter:
        """Build the per-thread connection adapter."""
        return _MySQLConnAdapter(self._new_raw_connection)

    def initialize(self) -> None:
        """Open the connection, create schema if absent, check/migrate version.

        Idempotent. No flock seeding and no forward-catch-up: in MySQL the event
        row and the mutation commit in ONE ACID transaction, so they can never
        skew (spec §2.7). The local ``events.jsonl`` is a per-host audit shadow,
        not the id authority.
        """
        if self._conn is not None:
            self._check_schema_version_mysql()
            return
        self._conn = self._connect()
        self._apply_ddl()
        self._check_schema_version_mysql()
        self._ensure_event_counter()

    def _apply_ddl(self) -> None:
        """Create every table (IF NOT EXISTS — idempotent)."""
        conn = self._require_conn()
        for stmt in DDL_MYSQL:
            conn.execute(stmt)

    def _ensure_event_counter(self) -> None:
        """Seed the single-row ``event_counter`` to ``MAX(events.id)`` ordinal.

        The counter is the cross-connection id authority (spec §2.3 option B):
        ``append`` takes a ``SELECT n ... FOR UPDATE`` row lock on it, so two
        racing appends on separate connections allocate DISTINCT, strictly
        increasing ids — no ``E000005`` collision on the events PK. Seeding from
        the existing events table makes it correct after a crash or a replay
        rebuild.
        """
        conn = self._require_conn()
        row = conn.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(id, 2) AS INTEGER)), 0) FROM events"
        ).fetchone()
        current_max = int(row[0]) if row and row[0] is not None else 0
        # INSERT the singleton row if absent; otherwise raise it to the events
        # max if the table is ahead (recovery). The id column is the constant 1.
        conn.execute(
            "INSERT INTO event_counter (id, n) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET n = GREATEST(n, excluded.n)",
            (current_max,),
        )

    def _check_schema_version_mysql(self) -> None:
        """Stamp/verify the one-row ``schema_version`` table (replaces PRAGMA).

        - empty table → fresh DB → INSERT ``SCHEMA_VERSION``.
        - present and ``== SCHEMA_VERSION`` → done.
        - present and ``< SCHEMA_VERSION`` → run the migration ladder. MySQL
          ships at v5 from day one, so the only path today is the no-op
          empty → v5; per-step ALTER branches are added as future versions land.
        - present and ``> SCHEMA_VERSION`` → SchemaMismatch.
        """
        conn = self._require_conn()
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            return
        on_disk = int(row[0])
        if on_disk == SCHEMA_VERSION:
            return
        if on_disk < SCHEMA_VERSION:
            # Future migration ladder lives here (ALTER TABLE ... with errno
            # 1060 duplicate-column tolerance). v5 is the floor today.
            conn.execute("DELETE FROM schema_version")
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            return
        raise SchemaMismatch(
            f"Database schema version {on_disk} does not match expected version "
            f"{SCHEMA_VERSION}. This code is older than the database."
        )

    def get_schema_version(self) -> int:
        """Return the version stamped in the ``schema_version`` table (0 if absent)."""
        conn = self._require_conn()
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return int(row[0]) if row else 0

    def discard_thread_connection(self) -> None:
        """Release the CURRENT thread's PyMySQL connection (idempotent).

        Each thread lazily opens its own connection on first use (so threads
        genuinely contend in InnoDB — spec §0). A long-lived thread pool or a
        test harness that spawns many short-lived worker threads should call
        this when a worker is done, or per-thread connections accumulate and can
        exhaust ``max_connections``.
        """
        if self._conn is not None:
            self._conn.discard_thread_connection()

    def close(self) -> None:
        """Close every PyMySQL connection this backend opened. Idempotent."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def _require_conn(self) -> _MySQLConnAdapter:  # type: ignore[override]
        if self._conn is None:
            raise RuntimeError(
                "MySQLBackend.initialize() must be called before any query or mutation."
            )
        return self._conn

    # ------------------------------------------------------------------
    # Core mutation — write path (seam 2, 3, 7). No flock; InnoDB owns it.
    # ------------------------------------------------------------------

    def append(self, draft: Any) -> Any:
        """Validate, allocate a DB-owned id, write event + mutation atomically.

        Differs from the SQLite ``append`` in exactly the spec-mandated ways:

        - No flock and no ``events.jsonl``-as-id-authority. The event row's id
          is allocated by the DB (AUTO_INCREMENT-style; here via a serialized
          ``MAX``+1 under the same transaction's row locks) so it is globally
          unique and monotonic across hosts.
        - The event INSERT and the projection mutation share ONE InnoDB
          transaction, so the SQLite "log line remains, projection rolled back"
          skew window cannot exist.
        - Validation (``_check_*``) runs INSIDE the transaction, after the
          contended rows have been locked by ``_check_claim_created``'s
          ``FOR UPDATE`` (spec §2.2), so the overlap re-checks read post-commit
          state — the InnoDB analog of the SQLite WAL-snapshot refresh.

        A clean ``EventRejected`` writes one ``rejection`` audit line and
        re-raises; an ``IdempotentNoOp`` writes an ``idempotent_no_op`` line and
        returns ``None``; a UNIQUE violation (the claim backstop) is translated
        to ``EventRejected``; a lock-wait timeout / persistent deadlock becomes
        ``StateLocked``.
        """
        from anvil.state.models import Event

        attempt = 0
        while True:
            try:
                return self._append_once(draft, Event)
            except _RetryableDeadlock:
                attempt += 1
                if attempt > _DEADLOCK_RETRIES:
                    raise StateLocked(
                        "append: InnoDB deadlock persisted after "
                        f"{_DEADLOCK_RETRIES} retries."
                    ) from None
                # else loop and retry — deadlock victims are safe to retry.

    def _append_once(self, draft: Any, event_cls: Any) -> Any:
        # NO process-wide lock here: each thread uses its own connection (the
        # adapter is per-thread), so concurrent appends genuinely contend in
        # InnoDB — exactly the cross-host shape. A process-wide lock would
        # silently relocate the single-winner guarantee back to a host-local
        # lock (spec §0).
        conn = self._require_conn()
        action = draft.action
        dispatch = self._get_action_dispatch()
        if action not in dispatch:
            reason = f"append: action {action!r} is not in the dispatch table."
            self._append_audit_line("rejection", draft, reason)
            raise EventRejected(reason)
        spec = dispatch[action]
        try:
            typed_payload = spec.payload_model.model_validate(draft.payload_json)
        except Exception as exc:
            reason = f"payload validation failed for action {action!r}: {exc}"
            self._append_audit_line("rejection", draft, reason)
            raise EventRejected(reason) from exc

        # Phase 0: allocate the event id in its OWN committed transaction,
        # BEFORE the claim transaction starts. The event_counter is the single
        # global contention point; if we locked it LAST inside the claim
        # transaction (while already holding the task-row + active-claim locks)
        # it would deadlock under multi-task contention. Allocating + committing
        # it up front releases the counter lock immediately, so the claim
        # transaction never holds it. A rolled-back claim leaves a gap in the id
        # sequence — fine: the SQLite scheme also tolerates gaps after an abort.
        next_n = self._allocate_event_n_committed()
        event_id = f"E{next_n:06d}"
        event = event_cls(id=event_id, **draft.model_dump())

        conn.execute("START TRANSACTION")
        try:
            # Phase 1: validation. _check_* takes the contended row locks
            # (FOR UPDATE) for claim.created; for other actions it is a
            # read-only check. Either way it runs inside this transaction.
            try:
                # The adapter duck-types sqlite3.Connection for the inherited
                # _check_*/_write_* code; the dispatch fns are typed against the
                # real sqlite3.Connection, hence the arg-type ignores here.
                spec.check(conn, typed_payload, draft)  # type: ignore[arg-type]
            except EventRejected as exc:
                conn.rollback()
                self._append_audit_line("rejection", draft, str(exc))
                raise
            except _IdempotentSignal as exc:
                conn.rollback()
                self._append_audit_line("idempotent_no_op", draft, str(exc))
                return None

            # Phase 2: mutation + event row, one atomic unit.
            spec.write(conn, typed_payload, event)  # type: ignore[arg-type]
            self._insert_event_row(conn, event, seq=None)  # type: ignore[arg-type]
            conn.commit()
        except EventRejected:
            raise
        except Exception as exc:
            conn.rollback()
            translated = self._translate_db_exception(exc, event_id=None)
            if isinstance(translated, _RetryableDeadlock):
                raise translated from exc
            if isinstance(translated, EventRejected):
                self._append_audit_line("rejection", draft, str(translated))
                raise translated from exc
            self._append_audit_line("write_failed_after_log", draft, str(exc))
            raise translated from exc

        # Phase 4: append the local audit-shadow JSONL line AFTER commit. For
        # MySQL the DB is the source of truth; a failure to write the shadow
        # must not roll back a committed claim, so it is best-effort. The
        # _shadow_lock serializes only the file append across threads (not the
        # DB transaction) so the JSONL lines are not interleaved/torn.
        try:
            with self._shadow_lock:
                self._append_audit_shadow(event)
        except OSError as exc:  # pragma: no cover - best effort
            logger.warning(
                "append: committed event %s but failed to write audit "
                "shadow line: %s",
                event_id,
                exc,
            )
        return event

    def _allocate_event_n_committed(self) -> int:
        """Atomically allocate + commit the next event ordinal (no held lock).

        Uses MySQL's ``LAST_INSERT_ID(expr)`` idiom on the single
        ``event_counter`` row::

            UPDATE event_counter SET n = LAST_INSERT_ID(n + 1) WHERE id = 1;
            SELECT LAST_INSERT_ID();

        The connection is ``autocommit=True``, so the UPDATE commits immediately
        and the row lock is released at once — the counter is NOT held across the
        claim transaction, which is what avoids the multi-task deadlock (the
        counter is the single global contention point; holding it while also
        holding the task-row + active-claim locks would create a lock cycle).
        ``LAST_INSERT_ID`` is per-connection, so concurrent appends on separate
        connections each read back THEIR OWN allocated value — distinct and
        strictly increasing. A rolled-back claim leaves a gap; that is fine (the
        SQLite scheme also tolerates gaps after an abort).
        """
        conn = self._require_conn()
        affected = conn.execute(
            "UPDATE event_counter SET n = LAST_INSERT_ID(n + 1) WHERE id = 1"
        )
        # If the singleton row was somehow missing, seed it and retry once.
        # (execute returns the adapter, not a rowcount; detect via a follow-up.)
        row = conn.execute("SELECT LAST_INSERT_ID()").fetchone()
        n = int(row[0]) if row else 0
        if n == 0:
            conn.execute(
                "INSERT INTO event_counter (id, n) VALUES (1, 1) "
                "ON CONFLICT(id) DO UPDATE SET n = LAST_INSERT_ID(n + 1)"
            )
            row = conn.execute("SELECT LAST_INSERT_ID()").fetchone()
            n = int(row[0]) if row else 1
        _ = affected
        return n

    def _append_audit_shadow(self, event: Any) -> None:
        """Write the event as a JSONL line to the local ``events.jsonl`` shadow."""
        line = self._serialize_event_line(event)
        with open(self._events_path, "a", encoding="utf-8") as fh:
            fh.write(line)

    def _translate_db_exception(
        self, exc: Exception, *, event_id: str | None
    ) -> Exception:
        """Map a PyMySQL error to the Protocol's exception surface (spec §2.7)."""
        errno = _pymysql_errno(exc)
        if errno == _ERR_DUP_ENTRY:
            # The uq_one_active_claim_per_task backstop (or any PK/UNIQUE
            # violation): a second active claim slipped past FOR UPDATE. This
            # is the engine enforcing single-winner — a clean rejection.
            return EventRejected(
                f"claim.created: rejected by the database single-winner "
                f"constraint (a competing active claim already exists): {exc}"
            )
        if errno == _ERR_DEADLOCK:
            return _RetryableDeadlock(str(exc))
        if errno == _ERR_LOCK_WAIT_TIMEOUT:
            return StateLocked(
                f"append: InnoDB lock wait timed out (another writer held the "
                f"row lock too long): {exc}"
            )
        return TransactionAborted(
            f"Transaction aborted for event {event_id!r}: {exc}"
        )

    # ------------------------------------------------------------------
    # Claim check-and-write atomicity (seam 2) — the load-bearing override.
    # ------------------------------------------------------------------

    def _check_claim_created(self, conn: Any, payload: Any, event: Any) -> None:
        """Take InnoDB row locks, then run the inherited claim guards.

        Replaces the SQLite ``BEGIN IMMEDIATE`` WAL-snapshot-refresh trick with
        explicit ``SELECT ... FOR UPDATE`` (spec §2.1/§2.2): we are already
        inside ``append``'s ``START TRANSACTION``; here we

          1. lock the contended task row (the primary same-task serialization
             point — cheap, single-row, PK-indexed), and
          2. lock the full ``active`` claim set (small — bounded by agent count)
             so the cross-task file/group overlap re-checks read post-commit
             state.

        The inherited ``_validate_claim_created_locked`` then runs the exact
        same Python guards as SQLite (status, same-task, file-overlap,
        group-overlap). A second claim that somehow races past these still hits
        the ``uq_one_active_claim_per_task`` UNIQUE on INSERT — the
        host-independent backstop that turns it into a clean ``EventRejected``.
        """
        _ = event
        # 1. Lock the task row (NULL row → guard below reports task-not-found).
        conn.execute(
            "SELECT status FROM tasks WHERE id = ? FOR UPDATE", (payload.task_id,)
        )
        # 2. Lock the active-claim set we reason about in the overlap re-checks.
        conn.execute(
            "SELECT id, claimed_by, task_id, expected_files "
            "FROM claims WHERE status = 'active' FOR UPDATE"
        )
        # 3. Run the inherited guards against the now-locked rows.
        self._validate_claim_created_locked(conn, payload)

    # ------------------------------------------------------------------
    # Replay (seam 7) — DB is the source of truth; rebuild from events.jsonl.
    # ------------------------------------------------------------------

    def replay_from_empty(self, events_path: str) -> None:
        """Drop + recreate all tables, then replay every JSONL line via _write_*.

        Same strict no-skip semantics as the SQLite local path: every interior
        line is a fact (a malformed one raises); only a torn trailing line is
        tolerated. Keeps the audit-guarantee primitive (events.jsonl → identical
        DB) working for MySQL — useful for disaster recovery and for
        cross-checking the JSONL shadow against the live DB.
        """
        import json

        from anvil.state.models import Event

        self.initialize()
        conn = self._require_conn()
        # Drop in FK-safe order via a disabled FK check window.
        conn.execute("SET FOREIGN_KEY_CHECKS = 0")
        try:
            for table in (
                "sync_mappings",
                "evidence",
                "claims",
                "tasks",
                "reviews",
                "events",
                "decisions",
                "conflict_groups",
                "requirements",
                "features",
                "prds",
                "projects",
                "schema_version",
                "event_counter",
            ):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        finally:
            conn.execute("SET FOREIGN_KEY_CHECKS = 1")
        self._apply_ddl()
        self._check_schema_version_mysql()

        if not os.path.exists(events_path):
            self._ensure_event_counter()
            return
        with open(events_path, encoding="utf-8") as fh:
            lines = fh.readlines()
        self._replaying = True
        try:
            for i, raw_line in enumerate(lines):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                is_last = i == len(lines) - 1
                try:
                    raw = json.loads(stripped)
                    event = Event.model_validate(raw)
                except Exception as exc:
                    if is_last:
                        logger.debug(
                            "replay_from_empty: skipping torn trailing line "
                            "(line %d): %s",
                            i + 1,
                            exc,
                        )
                        break
                    raise ValueError(
                        f"replay_from_empty: malformed interior line {i + 1}: {exc}"
                    ) from exc
                self._apply_write_only(conn, event)
        finally:
            self._replaying = False
        # Re-seed the id counter from the rebuilt events table so post-replay
        # appends continue the monotonic id sequence.
        self._ensure_event_counter()

    def _apply_write_only(self, conn: Any, event: Any, *, seq: int | None = None) -> None:
        """Apply one event via ``_write_*`` only — no validation, no logging.

        MySQL variant of the parent helper: wraps each event in its own InnoDB
        transaction and maps failures to the Protocol exceptions.
        """
        action = event.action
        dispatch = self._get_action_dispatch()
        if action not in dispatch:
            raise TransactionAborted(
                f"_apply_write_only: unsupported action {action!r} during replay."
            )
        spec = dispatch[action]
        try:
            typed_payload = spec.payload_model.model_validate(event.payload_json)
        except Exception as exc:
            raise TransactionAborted(
                f"_apply_write_only: payload parse failed for {action!r}: {exc}"
            ) from exc
        conn.execute("START TRANSACTION")
        try:
            spec.write(conn, typed_payload, event)
            self._insert_event_row(conn, event, seq=seq)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise TransactionAborted(
                f"Transaction aborted during replay of event {event.id!r}: {exc}"
            ) from exc

    def _safe_rollback(self, conn: Any) -> None:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Row converters — strip MySQL-only columns the models forbid as extras.
    # ------------------------------------------------------------------

    def _row_to_claim(self, row: Any) -> Any:
        """Drop the generated ``active_task_id`` column, then reuse the parent.

        ``SELECT *`` on the claims table returns the STORED generated column
        ``active_task_id`` (the single-winner UNIQUE backstop), but the Claim
        Pydantic model forbids extra fields. Strip it before delegating to the
        inherited converter so the model layer stays untouched.
        """
        import json

        from anvil.state.models import Claim

        d = dict(row)
        d.pop("active_task_id", None)
        if isinstance(d.get("expected_files"), str):
            d["expected_files"] = json.loads(d["expected_files"])
        return Claim.model_validate(d)


# ---------------------------------------------------------------------------
# Internal control-flow signals
# ---------------------------------------------------------------------------


class _RetryableDeadlock(Exception):
    """Internal: an InnoDB deadlock (errno 1213) the append loop should retry."""


# ``IdempotentNoOp`` is imported lazily by name here to avoid widening the public
# import surface; the inherited ``_check_*`` raises the real class.
from anvil.state.backend import IdempotentNoOp as _IdempotentSignal  # noqa: E402


def _pymysql_errno(exc: Exception) -> int | None:
    """Extract the MySQL errno from a PyMySQL exception, or None."""
    args = getattr(exc, "args", None)
    if args and isinstance(args[0], int):
        return args[0]
    return None
