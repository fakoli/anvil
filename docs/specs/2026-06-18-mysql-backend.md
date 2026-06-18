# Spec: MySQL Backend for Anvil

**Status:** PROPOSED (implemented behind the existing Backend Protocol)
**Date:** 2026-06-18
**Owner:** anvil
**Scope:** Additive. SQLite remains the default and ships unchanged. PyMySQL-only, no ORM, no Aurora-specific code.

## 0. The one load-bearing risk (read this first)

Anvil's single-winner-per-task guarantee on SQLite is **not** a pure database
property. It is produced by three things working together, two of which are
*host-process* primitives, not database primitives:

1. an `flock(LOCK_EX)` on `events.jsonl` — serializes appends **across OS
   processes on one host** (`SqliteBackend._append_lock`, `sqlite.py`);
2. an in-process `threading.Lock` (`_proc_lock`) — serializes appends **across
   threads in one process**;
3. `BEGIN IMMEDIATE` inside `_check_claim_created` — used *only* to force a
   fresh WAL read snapshot so the in-transaction overlap re-check sees the
   winner's already-committed row.

The actual rejection of a second claim is done in Python:
`_validate_claim_created_locked` runs
`SELECT ... FROM claims WHERE task_id=? AND status='active' AND id!=?` and raises
`EventRejected` if a row exists. That read is only trustworthy because the flock
guarantees no other writer is between its own check and commit. **The database
is not enforcing single-winner — the flock is.**

On a shared MySQL/Aurora server reached by N agents on N machines, `flock` on a
local file is meaningless: each host has its own `events.jsonl` and its own
flock. The threading.Lock only covers one process. **If we port the logic
literally, the single-winner guarantee silently evaporates and two agents claim
the same task.** This is the single thing that must be gotten right, and it is
the reason the dialect mapping below does not just translate SQL — it relocates
the serialization point from the filesystem into the database.

The 12-thread contract test (`tests/test_claims_concurrency.py`) only exercises
the *one-process / many-threads* shape, so it would pass against a naive MySQL
port while the real cross-host bug ships. The test plan (§4) addresses this with
a MySQL-only **multi-process** single-winner test, since separate processes each
open their own connection — the cross-host shape.

## 1. Config selection

Two additive fields on the frozen `Config` dataclass (`bin/src/anvil/config.py`),
mirroring the existing `durable_store` / `events_storage` literal-field pattern
(validated via `_validate_literal`, absent key → default, invalid value → loud
`ValueError` at load time):

```python
backend: Literal["sqlite", "mysql"] = "sqlite"   # DEFAULT unchanged
mysql_dsn: str | None = None                      # required iff backend == "mysql"
```

- `backend: sqlite` (the default, and the value for every config written before
  this key existed) selects `SqliteBackend` with byte-for-byte current behavior.
  No code path changes for existing users.
- `backend: mysql` requires `mysql_dsn`. Validated at load time the same way
  `durable_store == "s3"` requires `s3_bucket`.

**DSN format.** A standard SQLAlchemy-style URL parsed with the stdlib
`urllib.parse` (no SQLAlchemy dependency — we only read the parts and hand them
to `pymysql.connect`):

```
mysql://anvil:secret@db.internal:3306/anvil_state?charset=utf8mb4&ssl_ca=/etc/ssl/rds.pem
```

- Password may be omitted from the DSN and supplied via the
  `ANVIL_MYSQL_PASSWORD` env var (do not bake secrets into committed config).
- `urlparse` yields `username`, `password`, `hostname`, `port` (default 3306),
  `path` (→ database name), and query params (`charset`, `ssl_ca`,
  `connect_timeout`).

**Aurora MySQL needs zero new code.** Aurora MySQL is wire-compatible with
MySQL 8.0; PyMySQL talks to it unchanged. Aurora is selected purely by pointing
`mysql_dsn` at the cluster's **writer** endpoint. All writes MUST go to the
writer endpoint, never a reader endpoint — replica lag would let two agents read
a stale "no active claim" state and both win (the §0 risk in a different
costume). No Aurora-aware routing logic exists.

**Factory wiring.** The two existing factories (`cli/_helpers._open_backend` and
`mcp_server._open_backend`) gain a single branch, fed by a new `read_backend()`
soft-loader that follows the exact same soft-load-with-fallback contract as
`read_events_storage`: missing file → `("sqlite", None)`; unparseable/non-mapping
→ warn + `("sqlite", None)`; valid mapping with an invalid `backend` value →
raise; `backend: mysql` with a blank `mysql_dsn` → raise. The factory then
constructs either backend and calls `.initialize()`. Both satisfy the `Backend`
Protocol, so every downstream call site is untouched; the factory return type
widens from `SqliteBackend` to `Backend`.

## 2. Dialect mapping from the SQLite impl

`MySQLBackend` is a **new sibling class** in `bin/src/anvil/state/mysql.py`. It
**subclasses `SqliteBackend`** and reuses, verbatim, the `_check_*` / `_write_*`
decide/apply split, the `ActionSpec` dispatch table, the query helpers, and the
row → model converters — that domain logic is dialect-neutral and is the bulk of
`sqlite.py`. What changes is the seven infrastructure seams below.

The inherited code is reused unchanged via a tiny connection adapter
(`_MySQLConnAdapter`) that makes a PyMySQL connection quack like a
`sqlite3.Connection`: it routes every SQL string through a translator
(`_translate_sql` / `_translate_params`) that swaps `?`→`%s` and `:name`→
`%(name)s` placeholders and rewrites the handful of SQLite-only idioms, and it
returns hybrid rows supporting BOTH `dict(row)` and `row[i]` (the inherited
converters use both). The SQLite-specific functions are enumerated and mapped
explicitly below.

### 2.1 `BEGIN IMMEDIATE` → InnoDB transaction + row locking

| SQLite | MySQL (InnoDB) |
|---|---|
| `BEGIN IMMEDIATE` — acquires the db-level write lock up front | `START TRANSACTION` (InnoDB, `REPEATABLE READ`) with explicit `SELECT ... FOR UPDATE` to take the row locks the SQLite write-lock implied |
| `PRAGMA busy_timeout = 5000` | `SET SESSION innodb_lock_wait_timeout = 5` |
| `PRAGMA journal_mode = WAL` | **dropped** — InnoDB's redo log is the always-on equivalent (§2.6) |
| `PRAGMA synchronous = FULL/NORMAL` | `innodb_flush_log_at_trx_commit = 1` (strict) vs `= 2` (relaxed), set best-effort `SET GLOBAL` (it is a GLOBAL-only var in MySQL 8); it is a server-wide durability policy, NOT the single-winner primitive |
| `PRAGMA foreign_keys = ON` | InnoDB FKs are always enforced — no-op |

SQLite's `BEGIN IMMEDIATE` locks the *whole database*. InnoDB locks at
row/index granularity, so we get *more* concurrency — but we must take the locks
explicitly with `FOR UPDATE`, because a bare `START TRANSACTION` under
`REPEATABLE READ` would let two transactions each read "no active claim" against
their own consistent snapshot and both proceed. That is the MySQL analog of the
stale-WAL-snapshot problem the SQLite code solves with its `BEGIN IMMEDIATE`
refresh trick — same shape (take the write lock before reading the contended
rows), different mechanism.

### 2.2 Claim check-and-write atomicity — the decision

**Decision: do both. The `uq_one_active_claim_per_task` UNIQUE constraint is the
correctness backstop; `SELECT ... FOR UPDATE` is the clean serialization +
readable rejection path. The UNIQUE constraint is the load-bearing half.**

- **Why the UNIQUE constraint is non-negotiable.** The §0 risk is that the
  single-winner guarantee currently lives in an flock the database can't see. A
  partial unique index makes the database itself reject a second active claim,
  so correctness no longer depends on any host-side lock. MySQL 8.0 lacks
  partial indexes, but the standard idiom is a **STORED generated column**:

  ```sql
  ALTER TABLE claims
    ADD COLUMN active_task_id VARCHAR(64)
      AS (CASE WHEN status = 'active' THEN task_id ELSE NULL END) STORED,
    ADD UNIQUE KEY uq_one_active_claim_per_task (active_task_id);
  ```

  NULLs are not compared by a UNIQUE key, so any number of
  released/stale/force_released claims coexist, but **at most one `active` row
  per `task_id` can exist** — enforced by the engine, across all hosts, with no
  flock. (`MySQLBackend._row_to_claim` strips this generated column before
  Pydantic validation, since the Claim model forbids extras.)

- **Why `FOR UPDATE` is still added.** It preserves the existing clean-loser
  contract the test suite asserts (`_assert_no_dirty_errors`) by rejecting the
  loser *before* the write rather than via an exception-after-write. The write
  path is:

  ```sql
  START TRANSACTION;
  SELECT status FROM tasks WHERE id = %s FOR UPDATE;            -- lock the task row
  SELECT id, claimed_by, task_id, expected_files
    FROM claims WHERE status = 'active' FOR UPDATE;             -- lock active claims
  -- run the inherited _validate_claim_created_locked guards on the locked rows
  -- INSERT the claim row; uq_one_active_claim_per_task is the final backstop
  -- UPDATE tasks SET status='claimed' WHERE id=%s AND status='ready'
  COMMIT;
  ```

- **Net:** UNIQUE constraint = the correctness guarantee (host-independent,
  engine-enforced); `FOR UPDATE` = clean serialization + readable rejection. A
  UNIQUE violation (errno 1062) is translated to `EventRejected` — defense in
  depth.

**Replacing the flock.** `MySQLBackend` does **not** use `flock` and does
**not** use a process-wide lock for cross-host (or cross-thread) correctness.
The InnoDB row locks + the UNIQUE backstop are the serialization primitive. The
connection adapter is **per-thread** (each thread lazily opens its own PyMySQL
connection), so concurrent threads contend in InnoDB exactly like two agents on
two hosts; a process-wide lock would silently relocate the guarantee back to a
host-local lock and reintroduce the §0 bug.

### 2.3 AUTO_INCREMENT / event-id assignment

The SQLite code assigns ids from the **log** (`events.jsonl`) under the flock.
For MySQL the cross-host serialization the flock provided moves into the
database, because `events.jsonl` is per-host and cannot be the shared id
authority across machines. The `E{N:06d}` id format and the "log is the id
authority" *model* are kept, but the authority is a one-row `event_counter`
table: `append` takes a `SELECT n ... FOR UPDATE` row lock on it (spec option
B), increments, and the lock releases on commit. Two racing appends on separate
connections therefore allocate DISTINCT, strictly-increasing ids — no
`events`-PK collision. The counter is seeded from `MAX(events.id)` at
`initialize()` / after a replay rebuild, so it is crash- and replay-correct.

The `events.jsonl` audit log is still written per-host as a local audit shadow
(the Protocol's append docstring promises a JSONL line), but it is **no longer
the id authority and no longer the cross-process serializer** for MySQL — the DB
is. The shadow write happens AFTER commit and is best-effort: the DB is the
source of truth, so a failed shadow write must not roll back a committed claim.

### 2.4 `PRAGMA user_version` → `schema_version` table

MySQL has no per-database integer slot, so a one-row `schema_version` table
replaces `PRAGMA user_version`. `get_schema_version()` reads it (default 0 when
absent). `initialize()`: `CREATE TABLE IF NOT EXISTS` all tables; read
`schema_version`; empty → fresh → stamp `SCHEMA_VERSION`; equal → done; lower →
run the migration ladder; higher → `SchemaMismatch`. **MySQL ships at v5 from
day one** (no v1→v4 history existed for MySQL), so the only ladder step today is
empty → v5; per-step `ALTER TABLE` branches (with errno-1060 duplicate-column
tolerance) are added as future versions land. `SCHEMA_VERSION` stays the single
source of truth in `schema.py`.

### 2.5 DDL type mapping

A second DDL generator (`generate_schema_sql_mysql()`) lives beside the SQLite
one, sharing `SCHEMA_VERSION`.

| SQLite | MySQL 8.0 |
|---|---|
| `TEXT PRIMARY KEY` | `VARCHAR(64) PRIMARY KEY` (TEXT cannot be a PK/indexed without a prefix length; ids are short, fixed-shape) |
| `TEXT` (free text) | `TEXT` / `LONGTEXT` |
| `TEXT` (JSON columns) | `LONGTEXT` — **not** native `JSON`, so values round-trip as JSON **strings** exactly like SQLite; the inherited row converters do their own `json.loads`, so the model layer is untouched |
| `INTEGER` (`derived`, `seq`) | `INT` |
| `CREATE INDEX IF NOT EXISTS` | indexes declared INLINE in `CREATE TABLE` (MySQL lacks a reliable `CREATE INDEX IF NOT EXISTS`); `CREATE TABLE IF NOT EXISTS` keeps the whole statement idempotent |
| `REFERENCES ... ON DELETE ...` | identical InnoDB FK syntax |
| `id GLOB 'E[0-9]*'` CHECK | dropped — the id format is produced by code; correctness does not depend on it |
| Charset | every table `DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin` — `utf8mb4_bin` so id/string comparisons are case- and accent-SENSITIVE, matching SQLite's default binary collation (a case-insensitive collation would make `T001`/`t001` collide and break the `id != %s` self-skip in the claim guard) |

### 2.6 No WAL

`PRAGMA journal_mode = WAL` has no MySQL analog and is simply not emitted.
InnoDB's redo log + MVCC already provide concurrent readers with a writer. The
WAL-snapshot-staleness reasoning that drives the `BEGIN IMMEDIATE`-refresh in
`_check_claim_created` is replaced by InnoDB's `REPEATABLE READ` + explicit
`FOR UPDATE` (§2.1/§2.2): we take the write locks before the contended reads, so
we read post-commit state — same end-property, different machinery.

### 2.7 Connections, lifecycle, replay, errors

- **Connection model.** One PyMySQL connection **per thread** (lazily opened,
  tracked so `close()` shuts them all; `discard_thread_connection()` releases
  the current thread's for long-lived pools / test harnesses). `autocommit=True`
  at the connection level (matching SQLite's `isolation_level=None`); `append`
  manages transactions explicitly with `START TRANSACTION` / `COMMIT` /
  `ROLLBACK`. Cross-process/cross-host safety comes from InnoDB, not any app
  lock.
- **`initialize()`** — open, set session vars, `CREATE TABLE IF NOT EXISTS` DDL,
  schema-version check, seed `event_counter`. No `_scan_tail_id` flock seeding
  and no forward-catch-up against `events.jsonl`: in MySQL the event row and the
  mutation commit in **one** ACID transaction, so they can never skew. (This
  also removes the `TransactionAborted` "log line remains, projection rolled
  back" window — for MySQL the event row and projection are one atomic unit.)
- **`replay_from_empty(events_path)`** — drop + recreate all tables, replay each
  JSONL line via `_write_*` only, same strict no-skip semantics (torn trailing
  line tolerated; interior malformed line raises). Re-seeds `event_counter` from
  the rebuilt events table.
- **`close()`** — close every per-thread PyMySQL connection, idempotent.
- **Exception mapping** — `IntegrityError` (1062, UNIQUE) → `EventRejected` (the
  claim backstop); lock-wait timeout (1205) → `StateLocked`; deadlock (1213) →
  retried up to 3× (InnoDB deadlock victims are safe to retry), then
  `StateLocked`; any other failure after the event INSERT → `TransactionAborted`.
  This preserves the exact exception surface the Protocol and the contract test
  depend on (`ClaimError` wraps `EventRejected` upstream in `ClaimManager`).

## 3. Driver: PyMySQL as an optional `[mysql]` extra

- **Driver:** `PyMySQL` — pure-Python, no C build step, no system
  `libmysqlclient`. Talks MySQL 8.0 and Aurora MySQL identically. **No
  SQLAlchemy, no ORM** — `MySQLBackend` issues parameterized SQL through
  `cursor.execute`, exactly as `SqliteBackend` does through `sqlite3`.
- **Packaging:** added to `bin/pyproject.toml` `[project.optional-dependencies]`,
  mirroring the existing `s3 = ["boto3>=1.26"]` pattern:

  ```toml
  mysql = ["PyMySQL>=1.1"]
  ```

  Install: `pip install 'anvil-state[mysql]'`. The default install pulls nothing
  new.
- **Import discipline:** `import pymysql` happens **lazily inside
  `MySQLBackend`**, never at module top level, so a default (SQLite) install
  never imports it. If `backend: mysql` is configured but PyMySQL is not
  installed, a clear actionable error is raised at `initialize()`
  ("`backend: mysql` requires `pip install 'anvil-state[mysql]'`").
- **TLS for Aurora/RDS:** PyMySQL accepts `ssl={"ca": "/path/rds-ca.pem"}`; the
  DSN's `ssl_ca` query param threads straight into that. No extra dependency.

## 4. Test plan

**Hard constraint: the SQLite suite stays 100% green and unchanged.** No edits
to the existing SQLite assertions, no edits to `schema.py`'s SQLite DDL, no
behavioral change to `SqliteBackend`. The MySQL path is purely additive.

### 4.1 Reuse the concurrency contract test, parameterized

`tests/test_claims_concurrency.py` is parameterized at a backend fixture
(`sqlite` always; `mysql` only when `MYSQL_TEST_URL` is set), with the
DB-poking helpers gaining a thin dialect shim. The winner-count assertions are
pure Python on the returned outcomes and do not change. On a developer laptop
with no MySQL, the SQLite params run and pass; the MySQL params show as
`skipped`, never `failed`. `pytest.importorskip("pymysql")` skips cleanly if the
extra is not installed.

### 4.2 The test that catches the §0 risk — multi-PROCESS, not just multi-thread

`tests/test_mysql_backend.py::test_mysql_single_winner_across_processes` spawns
the contenders as separate **OS processes** (`multiprocessing`), each opening its
**own** `MySQLBackend` connection against the shared `MYSQL_TEST_URL`, all racing
the same task. It asserts exactly one wins and the DB shows exactly one active
claim. This is the test the flock-based SQLite path cannot satisfy cross-host
and the UNIQUE-constraint MySQL path must. (Within one process, the per-thread
connection model already makes the 12-thread contract test contend in InnoDB,
not in a host-local lock — but the multi-process test is the unambiguous proof.)

### 4.3 Additional MySQL-targeted unit tests (all skip-gated)

- `initialize()` stamps `schema_version = SCHEMA_VERSION`; second `initialize()`
  is idempotent; a future-version DB raises `SchemaMismatch`.
- The `uq_one_active_claim_per_task` UNIQUE rejects a second active claim even
  when inserted directly (the FOR UPDATE path bypassed) → `IntegrityError`.
- `replay_from_empty(events.jsonl)` rebuilds a MySQL DB row-equivalent to one
  built by live `append()` calls.
- DSN parsing: password-from-env, `ssl_ca` threading, default port,
  missing-database / wrong-scheme error messages.
- A clear error when `backend: mysql` is configured but PyMySQL is absent.

### 4.4 CI

A skip-gated `test-mysql` job starts a Docker MySQL service, sets
`MYSQL_TEST_URL`, installs `anvil-state[mysql]`, and runs the parameterized
suite + the cross-process test. The default CI job runs unchanged with no MySQL
service and shows the MySQL params as skipped — guaranteeing the SQLite suite
stays green and the MySQL path is also continuously exercised.

## 5. Summary of changes (additive surface)

| Area | Change |
|---|---|
| `config.py` | + `backend` and `mysql_dsn` fields (literal-validated, default `sqlite`/`None`); + `read_backend()` soft-loader mirroring `read_events_storage` |
| `state/mysql.py` | **new** — `MySQLBackend(SqliteBackend)`; shares `_check_*`/`_write_*` via a per-thread SQL-translating connection adapter; InnoDB row locks + `uq_one_active_claim_per_task` UNIQUE replace flock; `event_counter` FOR UPDATE id allocator; `schema_version` table; no WAL |
| `state/schema.py` | + `generate_schema_sql_mysql()` / `DDL_MYSQL` beside the existing generator; `SCHEMA_VERSION` shared, unchanged |
| `cli/_helpers.py`, `mcp_server.py` | `_open_backend` gains one `if kind == "mysql"` branch; return type widens to `Backend` |
| `pyproject.toml` | + `mysql = ["PyMySQL>=1.1"]` optional extra (and PyMySQL in the dev group so skip-gated tests can run) |
| `tests/test_claims_concurrency.py` | backend fixture parameterized (sqlite always, mysql when `MYSQL_TEST_URL` set); existing SQLite assertions unchanged |
| `tests/test_mysql_backend.py` | **new** — cross-process single-winner test (the §0 guard) + skip-gated unit tests |

**Nothing about the SQLite default changes.** A user who never sets
`backend: mysql` gets byte-for-byte current behavior, current dependencies, and
a green test suite.
