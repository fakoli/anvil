"""SQLite backend implementing the Backend protocol.

WAL mode + JSONL audit log.  The replay guarantee:
    replay_from_empty(events.jsonl) → state.db identical to original run.

Events are serialised to JSONL *before* the SQLite mutation so that even a
process crash after the JSONL write but before the COMMIT leaves the log
in a state that can be replayed cleanly.

Phase 2 note: only 'project.created' and 'state.initialized' are routed;
Phase 3 extends routing with: prd.parsed, prd.reviewed, prd.approved,
feature.created, task.created, task.scored, task.expanded, task.status_changed.
Phase 4 extends routing with: claim.created, claim.released, claim.renewed,
claim.stale.
Phase 5 extends routing with: evidence.submitted, task.applied.
"""

from __future__ import annotations

try:
    import fcntl  # POSIX whole-file advisory locking (flock)
except ImportError:  # Windows: fcntl is POSIX-only.
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt  # Windows byte-range file locking (locking)
except ImportError:  # POSIX: msvcrt is Windows-only.
    msvcrt = None  # type: ignore[assignment]
import datetime
import json
import logging
import os
import random
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import BaseModel

from anvil.bundles.eligibility import analyze_bundle_graph
from anvil.state.backend import (
    BackendError,  # noqa: F401
    EventRejected,
    IdempotentNoOp,
    SchemaMismatch,
    StateLocked,
    TransactionAborted,
)
from anvil.state.hashing import hash_event_id
from anvil.state.models import (
    DEFAULT_PRD_ID,
    PRD,
    TERMINAL_BUNDLE_STATUSES,
    BundleClaim,
    BundleReviewPolicy,
    BundleReviewVerdict,
    BundleStatus,
    Claim,
    ClaimStatus,
    ConflictGroup,
    Event,
    EventDraft,
    ExecutionBundle,
    Feature,
    PRDAssumption,
    Project,
    Requirement,
    Review,
    ReviewDecision,
    Score,
    SyncMapping,
    Task,
)
from anvil.state.payloads import (
    ACTION_TO_PAYLOAD,
    BundleAgentObservedPayload,
    BundleCheckpointRecordedPayload,
    BundleClaimedPayload,
    BundleClaimReleasedPayload,
    BundleClaimRenewedPayload,
    BundleClaimStalePayload,
    BundleCreatedPayload,
    BundlePlanAcknowledgedPayload,
    BundleProgressNotedPayload,
    BundleReviewRecordedPayload,
    BundleStatusChangedPayload,
    BundleSupersededPayload,
    ClaimCreatedPayload,
    ClaimReleasedPayload,
    ClaimRenewedPayload,
    ClaimStalePayload,
    ConflictGroupUpsertedPayload,
    EvidenceSubmittedPayload,
    FeatureCreatedPayload,
    FeatureDeletedPayload,
    FileChangedPayload,
    PrdApprovedPayload,
    PrdDecisionResolvedPayload,
    PrdParsedPayload,
    PrdReviewedPayload,
    PrdRevisedPayload,
    ProgressNotedPayload,
    ProjectCreatedPayload,
    StateInitializedPayload,
    SyncMappingDeletedPayload,
    SyncMappingUpsertedPayload,
    TaskAppliedPayload,
    TaskCreatedPayload,
    TaskDeletedPayload,
    TaskExpandedPayload,
    TaskScoredPayload,
    TaskStatusChangedPayload,
    TaskSyncedFromRemotePayload,
)
from anvil.state.schema import DDL, SCHEMA_VERSION

if TYPE_CHECKING:
    from anvil.clock import Clock
    from anvil.state.models import Evidence

logger = logging.getLogger(__name__)

# Maps the raw ``task.applied`` outcome strings stored in the reviews table to
# their canonical ReviewDecision equivalents.  ``"rejected"`` maps to
# ``needs_changes`` because a rejected task immediately auto-promotes back to
# ``drafted`` for rework (see _handle_task_applied) — it is NOT the terminal
# ``reject`` decision that would permanently close the review.
_TASK_OUTCOME_TO_REVIEW_DECISION: dict[str, str] = {
    "accepted": ReviewDecision.approve,
    "rejected": ReviewDecision.needs_changes,
}

_BUNDLE_TRANSITIONS: dict[BundleStatus, frozenset[BundleStatus]] = {
    # Activation and supersession are projected only by their dedicated
    # bundle.claimed and bundle.superseded events.
    BundleStatus.planned: frozenset({BundleStatus.replan_required}),
    BundleStatus.active: frozenset(
        {
            BundleStatus.implemented_unreviewed,
            BundleStatus.replan_required,
        }
    ),
    BundleStatus.implemented_unreviewed: frozenset(
        {
            BundleStatus.active,
            BundleStatus.reviewed_unintegrated,
            BundleStatus.replan_required,
        }
    ),
    BundleStatus.reviewed_unintegrated: frozenset(
        {
            BundleStatus.active,
            BundleStatus.integrated,
            BundleStatus.replan_required,
        }
    ),
    BundleStatus.integrated: frozenset(
        {BundleStatus.merged, BundleStatus.completed, BundleStatus.replan_required}
    ),
    BundleStatus.merged: frozenset({BundleStatus.completed}),
    BundleStatus.replan_required: frozenset(
        {BundleStatus.planned, BundleStatus.active}
    ),
    BundleStatus.completed: frozenset(),
    BundleStatus.superseded: frozenset(),
}


# ``_check_*`` runs in append()'s validation phase and is handed the *draft*
# (no id assigned yet), so its third arg is ``EventDraft`` — a check must never
# read ``event.id``. ``_write_*`` runs post-id-assignment and receives the
# materialized ``Event``. Keeping these distinct lets mypy reject a check that
# touches ``.id`` instead of silencing it with ``# type: ignore``.
_CheckFn = Callable[["sqlite3.Connection", Any, "EventDraft"], None]
_WriteFn = Callable[["sqlite3.Connection", Any, "Event"], None]


class ActionSpec(NamedTuple):
    """Dispatch entry for one event action: payload model + decide/apply phases.

    SL1-RR-1 architecture move #1 — every dispatched action is split into a
    validation phase and an infallible mutation phase:

    - ``check(conn, payload, event)`` reads current state and raises
      :class:`EventRejected` on an illegal transition / bad payload, or
      :class:`IdempotentNoOp` on an already-satisfied request. It performs no
      writes.
    - ``write(conn, payload, event)`` performs the mutation and contains no
      validation that can raise a rejection — it assumes ``check`` passed.

    The production write path is ``append()``, which calls ``spec.check`` and
    ``spec.write`` directly within the flock critical section. On
    ``EventRejected``, ``append`` writes a rejection line to ``audit.jsonl``
    and re-raises. On ``IdempotentNoOp``, ``append`` writes an
    ``idempotent_no_op`` line to ``audit.jsonl`` and returns ``None``. No
    abort tombstones are written to ``events.jsonl``.
    """

    payload_model: type[BaseModel]
    check: _CheckFn
    write: _WriteFn


def _idempotent_no_op(
    reason: str,
    *,
    warn_action: str | None = None,
    warn_target_id: str | None = None,
) -> IdempotentNoOp:
    """Build an :class:`IdempotentNoOp` carrying optional warn-log metadata.

    Some legal-but-already-satisfied requests historically emitted a
    ``warn.idempotent_no_op`` JSONL line (already-released / already-stale
    claims, double-submitted evidence); others returned silently (a
    status_changed already at its target, a replayed claim.created). The
    ``warn_*`` attributes let ``_apply_mutation`` reproduce exactly the prior
    behavior for each case: when ``warn_action`` is set it re-emits the warn
    log, otherwise it returns silently. ``reason`` is the human-readable detail.
    """
    exc = IdempotentNoOp(reason)
    exc.warn_action = warn_action  # type: ignore[attr-defined]
    exc.warn_target_id = warn_target_id  # type: ignore[attr-defined]
    return exc


# ---------------------------------------------------------------------------
# flock contention backoff — used by _append_lock
# ---------------------------------------------------------------------------

# Overall contention budget matches SQLite's busy_timeout (5 s).
_FLOCK_TIMEOUT_S = 5.0
_FLOCK_BACKOFF_INITIAL_S = 0.010
_FLOCK_BACKOFF_CAP_S = 0.500
_FLOCK_BACKOFF_JITTER = 0.10


def _flock_backoff_delays(
    rand: Callable[[], float] = random.random,
) -> Iterator[float]:
    """Yield flock retry delays: exponential from 10 ms to a 500 ms cap, ±10% jitter.

    Fixed-interval polling starves late arrivals under a coordinated
    multi-agent wave: every waiter wakes on the same tick, the same few
    claimants win repeatedly, and the rest burn their whole 5 s budget in
    lockstep. Exponential growth keeps early retries cheap while the jitter
    de-synchronizes the wake-ups so each release is contested by waiters at
    staggered offsets.

    ``rand`` must return a uniform float in [0, 1); it is injectable so tests
    can pin the jitter (0.5 → exact midpoint schedule) without patching the
    ``random`` module.
    """
    base = _FLOCK_BACKOFF_INITIAL_S
    while True:
        yield base * (1.0 + _FLOCK_BACKOFF_JITTER * (2.0 * rand() - 1.0))
        base = min(base * 2.0, _FLOCK_BACKOFF_CAP_S)


# Byte offset for the Windows lock. msvcrt.locking takes a *mandatory* byte-range
# lock, so we lock a single sentinel byte far beyond any real events.jsonl content
# rather than byte 0 — that way the lock never blocks concurrent *readers* of the
# file's actual bytes, only other *appenders* taking the same sentinel lock.
_WIN_LOCK_OFFSET = 0x7FFF_0000


def _append_lock_acquire_nb(lock_fh: Any) -> None:
    """Take an exclusive, non-blocking, cross-process advisory lock on ``lock_fh``.

    Raises ``OSError`` when another process already holds the lock; callers treat
    that as contention and retry with backoff. POSIX uses ``fcntl.flock`` over the
    whole open file description; Windows uses ``msvcrt.locking`` over a single
    sentinel byte (see ``_WIN_LOCK_OFFSET``). When neither API exists this is a
    no-op and callers fall back to the in-process lock alone.
    """
    if fcntl is not None:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif msvcrt is not None:
        os.lseek(lock_fh.fileno(), _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(lock_fh.fileno(), msvcrt.LK_NBLCK, 1)


def _append_lock_release(lock_fh: Any) -> None:
    """Release the lock taken by :func:`_append_lock_acquire_nb` (best effort)."""
    try:
        if fcntl is not None:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(lock_fh.fileno(), _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def read_db_schema_version(db_path: str | os.PathLike[str]) -> int:
    """Read the on-disk ``PRAGMA user_version`` WITHOUT migrating.

    T007/B11 (MUST-FIX 2a): the TRUE pre-migration schema version stamped on a
    database. Unlike ``SqliteBackend.get_schema_version()`` — which reads the
    PRAGMA *after* ``initialize()`` has already migrated/re-stamped it to
    ``SCHEMA_VERSION`` — this is a lightweight standalone read that opens the db
    read-only and never runs DDL or migrations. That makes genuine drift
    (db_version < code_version, pre-migration) observable by the ``status``
    command instead of being masked by the open-time migration.

    Returns 0 when the db file does not exist (a brand-new db reports
    ``user_version == 0``), matching SQLite's own default for an unstamped db.
    """
    path = Path(db_path)
    if not path.exists():
        return 0

    def _read(conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    # Prefer a read-only open so a stale/un-migratable db is never mutated by a
    # status read, and so we observe its CURRENT version, not a migrated one.
    # A WAL-mode db with an uncheckpointed -wal sidecar can refuse mode=ro
    # ("unable to open database file") because read-only mode cannot create the
    # -shm it needs; fall back to a normal read connection in that case. We
    # still only run a single PRAGMA read and never any DDL/migration, so the
    # no-mutation intent holds either way.
    try:
        return _read(sqlite3.connect(f"file:{path}?mode=ro", uri=True))
    except sqlite3.OperationalError:
        return _read(sqlite3.connect(str(path)))


class SqliteBackend:
    """Concrete SQLite + JSONL implementation of the Backend protocol.

    Constructor parameters
    ----------------------
    db_path      : absolute path to the SQLite database file.
    events_path  : absolute path to the JSONL event-log file.
    clock        : Clock instance injected for all timestamp generation.
                   Never call datetime.now() directly in this class.
    durability   : ``"relaxed"`` (default) — synchronous=NORMAL, no per-event
                   fsync; ``"strict"`` — synchronous=FULL + fsync(log) before
                   COMMIT. See SL1-RR-1 spec section 6.
    events_storage : ``"local"`` (default) — monotonic E{N} event ids, strict
                   sequential replay, pre-1.22.0 behaviour byte-for-byte;
                   ``"git"`` — hash-chained event ids + Lamport counter in the
                   envelope and order-tolerant replay, so events.jsonl can be
                   committed and merged with ``merge=union`` (git-backed
                   events Phase A, docs/specs/2026-06-10-git-backed-events.md).
    sleep_fn     : injectable sleep used by the flock contention backoff in
                   ``_append_lock``. Defaults to ``time.sleep``; tests inject
                   a fake that advances a fake monotonic counter instead of
                   blocking.
    monotonic_fn : injectable monotonic time source for the ``_append_lock``
                   contention deadline. Defaults to ``time.monotonic`` — NOT
                   ``clock.now()``, which is wall-clock and would let an NTP
                   step stretch or shorten the 5 s timeout.

    Lifecycle (SL1-RR-1 write-path)
    ---------------------------------
    b = SqliteBackend(db_path=..., events_path=..., clock=...)
    b.initialize()   # open connection, set PRAGMAs, create schema,
                     # seed _next_seq from log max, forward catch-up if needed
    event = b.append(draft)  # validate → assign id → log-first → apply
    ...
    b.close()
    """

    def __init__(
        self,
        *,
        db_path: str,
        events_path: str,
        clock: Clock,
        durability: str = "relaxed",
        events_storage: str = "local",
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db_path = db_path
        self._events_path = events_path
        self._clock = clock
        self._durability = durability
        self._events_storage = events_storage
        self._sleep_fn = sleep_fn
        self._monotonic_fn = monotonic_fn
        self._conn: sqlite3.Connection | None = None
        # In-memory monotonic counter; seeded from log max on initialize().
        # Incremented at log-append time inside the flock critical section.
        # Local mode only — git mode derives ids from content hashes.
        self._next_seq: int = 0
        # Git mode (v1.22.0): Lamport high-water mark across every event this
        # process has seen — seeded by the full-log scan in initialize()/replay,
        # advanced on each append. The writer assigns max-seen + 1; ties across
        # writers are legal and broken deterministically at replay by (ts, id).
        self._max_lamport: int = 0
        # In-process threading lock nested inside the flock for same-process
        # MCP + CLI thread safety. The outer flock serializes cross-process
        # appends. This is re-entrant because higher-level operations such as
        # ClaimManager.claim() may serialize their pre-append reads on the same
        # lock, then call append(), whose _append_lock() takes it again.
        self._proc_lock = threading.RLock()
        # Set True during replay_from_empty and _forward_catch_up so that
        # _write_* methods with audit side-effects (e.g. _write_evidence_submitted)
        # suppress those writes — audit lines must not be appended during replay.
        self._replaying: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Open the SQLite connection, set PRAGMAs, apply DDL if needed.

        Idempotent — safe to call multiple times.  Raises SchemaMismatch if
        the on-disk user_version differs from SCHEMA_VERSION.

        SL1-RR-1 additions on open:
        1. Seed ``_next_seq`` from the log's max id (``scan_tail``). The log is
           the id authority; SQLite ``MAX(id)`` is NOT consulted.
        2. Forward catch-up: if the events table is behind the log (log-ahead
           skew from a previous crash), re-apply the missing tail via
           ``_write_*`` so the projection converges. This reuses the same
           ``_write_*`` path as ``replay_from_empty`` — there is no third
           apply implementation.

        Ordering note (P1-3): the pre-DDL ``user_version`` is captured
        BEFORE ``_apply_ddl`` runs. ``_apply_ddl`` no longer stamps the
        version (BUG 002 — the stamp is deferred to a successful
        ``_check_schema_version``), but the capture still happens up front so
        the migration ladder sees the true on-disk version. A torn migration
        therefore leaves ``user_version`` at the pre-migrate value for a clean
        retry on the next open, instead of looking "already current".
        """
        if self._conn is not None:
            # Already initialised — verify version and return.
            self._check_schema_version()
            return

        try:
            conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                isolation_level=None,  # autocommit off; we manage transactions explicitly
            )
        except sqlite3.OperationalError as exc:
            raise TransactionAborted(f"Cannot open database at {self._db_path!r}: {exc}") from exc

        # WAL mode for concurrent readers + one writer.
        conn.execute("PRAGMA journal_mode = WAL")
        # synchronous level is set by durability mode below.
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")

        # Row factory enables dict(row) in query helpers.
        conn.row_factory = sqlite3.Row

        self._conn = conn

        # Apply durability-mode synchronous pragma.
        if self._durability == "strict":
            conn.execute("PRAGMA synchronous = FULL")
        else:
            conn.execute("PRAGMA synchronous = NORMAL")

        # Capture the on-disk version before the migration ladder runs. The
        # migration ladder relies on knowing the original version. (_apply_ddl
        # no longer stamps user_version — BUG 002 — but capturing here is still
        # correct and keeps the read away from any DDL side effects.)
        pre_ddl_row = conn.execute("PRAGMA user_version").fetchone()
        pre_ddl_version = pre_ddl_row[0] if pre_ddl_row else 0

        # Apply DDL (CREATE TABLE IF NOT EXISTS — idempotent).
        # Execute statement-by-statement; sqlite3 executescript auto-commits,
        # so we split manually to preserve our transaction control.
        self._apply_ddl()

        # After DDL, verify schema version. Pass the pre-DDL version so the
        # migration logic can decide what (if any) ALTER steps are needed.
        self._check_schema_version(pre_ddl_version=pre_ddl_version)

        # SL1-RR-1: seed the in-memory counter from the log max (log is the
        # id authority; we never read SQLite MAX(id) for this purpose).
        log_max = self._scan_tail_id()
        self._next_seq = log_max

        # Forward catch-up: if projection is behind the log, re-apply the tail.
        # Suppress audit side-effects during catch-up (same contract as replay).
        # If _replaying is already True (we were called from replay_from_empty),
        # do not run catch-up — replay_from_empty will apply every event itself.
        # Running catch-up AND replay would apply events twice.
        if self._events_storage == "git":
            # Git mode (v1.22.0, Phase A): a `git pull`/merge can splice
            # events ANYWHERE into the file, not just at the tail, so the
            # local-mode assumption behind surgical catch-up — that the
            # projection is a strict prefix of the log — no longer holds.
            # Convergence is instead judged by event-id SET equality and
            # healed by a full order-tolerant rebuild (which is the only way
            # to apply a merged-in interior event at its correct HLC position).
            if not self._replaying:
                self._git_converge_projection()
        elif log_max > 0 and not self._replaying:
            table_max = self._table_max_id(conn)
            if table_max < log_max:
                self._replaying = True
                try:
                    self._forward_catch_up(conn, from_seq=table_max + 1, to_seq=log_max)
                finally:
                    self._replaying = False

    def close(self) -> None:
        """Close the SQLite connection cleanly.  Idempotent."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    # ------------------------------------------------------------------
    # Core mutation — SL1-RR-1 write path
    # ------------------------------------------------------------------

    def append(self, draft: EventDraft) -> Event | None:
        """Validate, assign id from log-authority counter, log-first, then apply.

        This is the sole production write entry point (SL1-RR-1). The entire
        critical section is guarded by an flock on ``events.jsonl`` (cross-process
        serialization) nested inside a threading.Lock (same-process serialization).

        Ordering inside the critical section:
          1. ``_check_<action>`` — raises ``EventRejected`` → audit rejection,
             re-raise; raises ``IdempotentNoOp`` → audit idempotent_no_op, return None.
          2. ``id = _next_seq()`` — increments the in-memory counter (log-owned).
             Counter increments at log-append time, not at COMMIT, so a re-run
             after a write failure gets the next id, and the failed event remains
             accounted-for in the log.
          3. Append the materialized Event line to ``events.jsonl`` (log-first).
          4. If ``durability="strict"``: fsync the log file before COMMIT.
          5. ``BEGIN IMMEDIATE; _write_<action>; _insert_event_row; COMMIT``.

        On write failure after log append (step 3 succeeded, step 5 raised):
          - ROLLBACK SQLite.
          - Leave the log line (append-only — do NOT truncate).
          - Write a ``write_failed_after_log`` line to ``audit.jsonl``.
          - Raise ``TransactionAborted``.
          - Forward catch-up on the next ``initialize()`` heals the skew.
        """
        conn = self._require_conn()

        with self._append_lock():
            # ---- Phase 1: validation (read-only) ----
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

            try:
                spec.check(conn, typed_payload, draft)
            except EventRejected as exc:
                reason = str(exc)
                self._append_audit_line("rejection", draft, reason)
                raise
            except IdempotentNoOp as exc:
                reason = str(exc)
                self._append_audit_line("idempotent_no_op", draft, reason)
                return None

            # Persist the canonical typed assumption records in new PRD events.
            # Older logs that omitted the additive field retain their original
            # shape and continue to replay as an empty assumption list.
            materialized_draft = draft
            if isinstance(typed_payload, (PrdParsedPayload, PrdRevisedPayload)) and (
                "assumptions" in draft.payload_json
            ):
                canonical_payload = dict(draft.payload_json)
                canonical_payload["assumptions"] = [
                    assumption.model_dump(mode="json")
                    for assumption in typed_payload.assumptions
                ]
                materialized_draft = draft.model_copy(
                    update={"payload_json": canonical_payload}
                )

            # ---- Phase 2: id assignment ----
            if self._events_storage == "git":
                # Git mode (v1.22.0): hash-chained id + Lamport counter. The
                # chain parent is the last event in FILE order as seen by this
                # writer — we hold the flock, so the tail is stable and covers
                # appends by other processes since we opened. The Lamport value
                # is max-seen + 1, where "seen" is the in-memory high-water
                # mark (full-log scan at initialize()/replay) reconciled with
                # the tail line. A merged-in INTERIOR line could in theory
                # carry a higher lamport than both — harmless: replay breaks
                # lamport ties deterministically by (ts, id), so the counter
                # only needs to be a causal lower bound, not a global maximum.
                parent_event_id, tail_lamport = self._scan_tail_envelope()
                event_id = hash_event_id(
                    parent_event_id=parent_event_id,
                    action=materialized_draft.action,
                    target_kind=materialized_draft.target_kind,
                    target_id=materialized_draft.target_id,
                    payload=materialized_draft.payload_json,
                    actor=materialized_draft.actor,
                    ts=materialized_draft.timestamp.isoformat(),
                )
                event = Event(
                    id=event_id,
                    parent_event_id=parent_event_id,
                    lamport=max(self._max_lamport, tail_lamport) + 1,
                    **materialized_draft.model_dump(),
                )
            else:
                # Local mode (log-owned counter). We are inside the flock, so
                # the log tail is the authoritative source of the maximum
                # assigned id.  Reconcile the in-memory counter with the log
                # before incrementing so that two separate processes that both
                # seeded _next_seq from the same stale log_max at initialize()
                # time do NOT assign the same id.  This is the PR #41 Critic-3
                # cross-process id-collision fix (SL1-RR-1).
                #
                # _scan_tail_id() is O(last-line) and already tolerates a torn
                # trailing line, so it is safe to call here under the flock.
                # The in-memory counter remains a valid fast-path for the
                # single-process case: if no other process has written since
                # our last append, scan_tail returns _next_seq and max() is a
                # no-op.
                self._next_seq = max(self._next_seq, self._scan_tail_id())
                self._next_seq += 1
                event_id = f"E{self._next_seq:06d}"
                event = Event(id=event_id, **materialized_draft.model_dump())

            # ---- Phase 3: log-first append ----
            event_line = self._serialize_event_line(event)
            try:
                with open(self._events_path, "a", encoding="utf-8") as log_fh:
                    log_fh.write(event_line)
                    # Phase 4: fsync before COMMIT in strict mode.
                    if self._durability == "strict":
                        log_fh.flush()
                        os.fsync(log_fh.fileno())
            except OSError as exc:
                # Log write failed before COMMIT — nothing was appended. In
                # local mode the counter was already incremented; reverse it
                # so the id is not orphaned (the log has no record of it).
                # Git mode mutates no counter before a successful write — the
                # hash id simply never entered the log.
                if self._events_storage != "git":
                    self._next_seq -= 1
                raise TransactionAborted(
                    f"append: failed to write event {event_id!r} to log: {exc}"
                ) from exc

            if self._events_storage == "git" and event.lamport is not None:
                # Advance the high-water mark HERE — after the log append, not
                # after COMMIT — because the event now exists in the log
                # regardless of how the SQLite mutation below fares, and the
                # next append's "max-seen" must account for it.
                self._max_lamport = event.lamport

            # ---- Phase 5: SQLite mutation ----
            try:
                conn.execute("BEGIN IMMEDIATE")
                spec.write(conn, typed_payload, event)
                # Git mode: a live append is, by construction, the newest
                # event this machine has seen, so it takes the next display
                # seq; a post-merge replay reassigns seq globally in HLC
                # order. Local mode leaves seq NULL — the monotonic id IS the
                # display order.
                self._insert_event_row(
                    conn,
                    event,
                    seq=(
                        self._next_display_seq(conn)
                        if self._events_storage == "git"
                        else None
                    ),
                )
                conn.execute("COMMIT")
            except Exception as exc:
                self._safe_rollback(conn)
                # After the log line has been written (step 3 succeeded), any
                # SQLite failure — including "database is locked" — is a genuine
                # post-log-append failure. The event id is already committed to
                # the log; a caller retry would write a NEW log line with a new
                # id, leaving this one as a phantom. Surface as
                # write_failed_after_log + TransactionAborted so the forward
                # catch-up on the next initialize() can heal the skew.
                #
                # StateLocked is only appropriate BEFORE the log append
                # (the flock-timeout path in _append_lock already handles that).
                # sqlite3.OperationalError is a subclass of Exception so the
                # single branch covers both the "database is locked" case and
                # any other unexpected mutation failure.
                self._append_audit_line(
                    "write_failed_after_log", draft, str(exc), event_id=event_id
                )
                raise TransactionAborted(
                    f"Transaction aborted for event {event_id!r} (log line remains): {exc}"
                ) from exc

        return event

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    def replay_from_empty(self, events_path: str) -> None:
        """Reconstruct state.db from events.jsonl. Strict no-skip replay.

        Steps (SL1-RR-1)
        ----------------
        1. Close and delete state.db (+ WAL/SHM sidecars).
        2. Re-open and re-create schema (call initialize()).
        3. Read every line of events_path. Every line is a canonical event fact —
           there is no action-name skip-list. Apply each via ``_write_*`` only
           (no validation, no JSONL logging).
        4. Torn trailing line (from a crash mid-append) is tolerated and skipped.
           Any interior malformed line raises — that is corruption, not a torn write.
        5. Re-seed ``_next_seq`` from the max id seen during replay.
        """
        if self._events_storage == "git":
            # v1.22.0 — order-tolerant replay; see _replay_from_empty_git.
            # The strict-sequence body below is the LOCAL path and stays
            # byte-for-byte as shipped (its replay byte-equality guarantee is
            # frozen per the git-backed-events spec, Risks table).
            self._replay_from_empty_git(events_path)
            return

        # Close existing connection.
        self.close()

        # Delete the database file (and any WAL/SHM sidecars).
        for suffix in ("", "-wal", "-shm"):
            path = self._db_path + suffix
            if os.path.exists(path):
                os.remove(path)

        # Re-open fresh.  initialize() will also seed _next_seq via scan_tail
        # and run forward catch-up — but since we are rebuilding from scratch
        # the catch-up will be a no-op (table_max == log_max after replay).
        # Set _replaying before initialize() so catch-up inside initialize()
        # also suppresses audit side-effects.
        self._replaying = True
        try:
            self.initialize()

            if not os.path.exists(events_path):
                return

            conn = self._require_conn()
            last_event_id = 0

            with open(events_path, encoding="utf-8") as fh:
                lines = fh.readlines()

            for i, raw_line in enumerate(lines):
                stripped = raw_line.strip()
                if not stripped:
                    continue

                # Determine if this is the last (possibly torn) line.
                is_last = i == len(lines) - 1

                try:
                    raw: dict[str, Any] = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    if is_last:
                        # Torn trailing line — tolerate silently.
                        logger.debug(
                            "replay_from_empty: skipping torn trailing line (line %d): %s",
                            i + 1,
                            exc,
                        )
                        break
                    # Interior malformed line — this is corruption.
                    raise ValueError(
                        f"replay_from_empty: malformed JSON on interior line {i + 1}: {exc}"
                    ) from exc

                try:
                    event = Event.model_validate(raw)
                except Exception as exc:
                    if is_last:
                        # Torn/corrupt trailing line — tolerate.
                        logger.debug(
                            "replay_from_empty: skipping invalid trailing event (line %d): %s",
                            i + 1,
                            exc,
                        )
                        break
                    raise ValueError(
                        f"replay_from_empty: cannot parse Event on interior line {i + 1}: {exc}"
                    ) from exc

                # Apply via _write_* only — no _check_*, no logging.
                self._apply_write_only(conn, event)

                # Track max id for counter re-sync.
                try:
                    seq = int(event.id[1:])
                    if seq > last_event_id:
                        last_event_id = seq
                except (ValueError, IndexError):
                    pass

            # Re-seed counter from the max id replayed. scan_tail already seeded it
            # from the log during initialize() above, but we keep it consistent with
            # what we actually replayed.
            if last_event_id > 0:
                self._next_seq = last_event_id
        finally:
            self._replaying = False

    def _replay_from_empty_git(self, events_path: str) -> None:
        """Order-tolerant rebuild for ``events_storage: git`` (v1.22.0 Phase A).

        Differences from the strict local replay, each forced by the
        ``merge=union`` git layout:

        1. **Dedupe by event id.** A union merge can duplicate a line (the
           same event present on both sides of the merge). Identical id ⇒
           identical content — the id is a content hash — so the first
           occurrence applies and the rest are skipped (idempotent replay).
        2. **Order by (lamport, ts, event_id)**, not file order. Line order
           across a merge point is arbitrary; the hybrid-logical-clock sort
           makes the rebuild deterministic no matter how two branches'
           suffixes interleaved. Competing events that do not commute (two
           ``claim.created`` on one task) therefore resolve the same way on
           every machine: earliest (lamport, ts, id) applies first and wins
           the task transition. (Materializing the loser as
           ``claim.superseded`` is the Phase B reconciler's job.)
        3. **seq assignment.** Events are applied in HLC order and numbered
           1..N into the projection's ``seq`` column — derived display state,
           never written back to the log.

        Torn-trailing-line tolerance matches the local path exactly: only the
        final line may fail to parse (crash mid-append); interior damage
        raises — that is corruption, not a torn write.
        """
        # Close and delete state.db (+ WAL/SHM sidecars), same as local replay.
        self.close()
        for suffix in ("", "-wal", "-shm"):
            path = self._db_path + suffix
            if os.path.exists(path):
                os.remove(path)

        # Set _replaying before initialize() so the git convergence check is
        # skipped (we ARE the convergence) and audit side-effects in _write_*
        # stay suppressed, same contract as the local path.
        self._replaying = True
        try:
            self.initialize()

            if not os.path.exists(events_path):
                return

            conn = self._require_conn()

            with open(events_path, encoding="utf-8") as fh:
                lines = fh.readlines()

            events_by_id: dict[str, Event] = {}
            for i, raw_line in enumerate(lines):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                is_last = i == len(lines) - 1
                try:
                    raw: dict[str, Any] = json.loads(stripped)
                    event = Event.model_validate(raw)
                except Exception as exc:
                    # JSON decode and envelope validation share one tolerance
                    # rule: a damaged FINAL line is a torn write, anything
                    # interior is corruption.
                    if is_last:
                        logger.debug(
                            "git replay: skipping torn trailing line (line %d): %s",
                            i + 1,
                            exc,
                        )
                        break
                    raise ValueError(
                        f"git replay: malformed event on interior line {i + 1}: {exc}"
                    ) from exc
                if event.id in events_by_id:
                    # merge=union duplicated this line — applying once is the
                    # correct semantics by construction (spec Risks table).
                    logger.debug("git replay: duplicate event %s skipped", event.id)
                    continue
                events_by_id[event.id] = event

            # Hybrid-logical-clock order. A missing lamport (hand-edited or
            # not-yet-migrated line) sorts first as 0 rather than crashing
            # the rebuild; timestamps are tz-aware (model-enforced) so the
            # datetime comparison is total.
            ordered = sorted(
                events_by_id.values(),
                key=lambda e: (e.lamport or 0, e.timestamp, e.id),
            )

            max_lamport = 0
            for seq, event in enumerate(ordered, start=1):
                self._apply_write_only(conn, event, seq=seq)
                if event.lamport is not None and event.lamport > max_lamport:
                    max_lamport = event.lamport
            self._max_lamport = max_lamport
        finally:
            self._replaying = False

    def replay_to_event_id(self, events_path: str, stop_after_event_id: str) -> None:
        """Reconstruct state.db AS OF a bounded prefix of the log (read-only).

        Like :meth:`replay_from_empty`, but stops after applying the event whose
        id equals ``stop_after_event_id`` — every event ordered after it is left
        unapplied. This rebuilds the projection exactly as it stood the instant
        that event committed, which is how a caller inspects a PRD at an earlier
        revision: replaying ``[prd.parsed(rev1), prd.revised(rev2)]`` and
        stopping after the ``prd.parsed`` id yields the rev1 live set; stopping
        after the ``prd.revised`` id yields the rev2 set.

        Bounded replay reuses the SAME ``_apply_write_only`` path and the SAME
        per-mode ordering / torn-trailing-line tolerance as
        :meth:`replay_from_empty`, so the rebuilt prefix is byte-identical to the
        prefix a full replay would produce — there is no second apply
        implementation and no divergence between the bounded and unbounded
        variants.

        - **Local mode**: events apply in file order (the canonical local order),
          stopping after the line whose id matches.
        - **Git mode**: events apply in hybrid-logical-clock order
          ``(lamport, ts, id)`` after deduping by id — identical to
          ``_replay_from_empty_git`` — stopping after the matching id in that
          order.

        Read-only: applying via ``_apply_write_only`` writes ONLY the projection
        (state.db); no new line is appended to events.jsonl. The counter
        (``_next_seq`` / ``_max_lamport``) is re-seeded from the events that were
        actually applied, so a subsequent append would continue from the bounded
        prefix — callers using this for inspection should treat the backend as
        a throwaway snapshot.

        Raises ``ValueError`` if ``stop_after_event_id`` is not found in the log
        (an unbounded replay would have applied a different set, so silently
        replaying everything would be a lie about the bound).
        """
        if self._events_storage == "git":
            self._replay_to_event_id_git(events_path, stop_after_event_id)
            return

        # Close existing connection.
        self.close()

        # Delete the database file (and any WAL/SHM sidecars).
        for suffix in ("", "-wal", "-shm"):
            path = self._db_path + suffix
            if os.path.exists(path):
                os.remove(path)

        self._replaying = True
        try:
            self.initialize()

            if not os.path.exists(events_path):
                raise ValueError(
                    f"replay_to_event_id: events.jsonl does not exist but "
                    f"stop_after_event_id {stop_after_event_id!r} was requested."
                )

            conn = self._require_conn()
            last_event_id = 0
            found = False

            with open(events_path, encoding="utf-8") as fh:
                lines = fh.readlines()

            for i, raw_line in enumerate(lines):
                stripped = raw_line.strip()
                if not stripped:
                    continue

                is_last = i == len(lines) - 1

                try:
                    raw: dict[str, Any] = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    if is_last:
                        logger.debug(
                            "replay_to_event_id: skipping torn trailing line "
                            "(line %d): %s",
                            i + 1,
                            exc,
                        )
                        break
                    raise ValueError(
                        f"replay_to_event_id: malformed JSON on interior line "
                        f"{i + 1}: {exc}"
                    ) from exc

                try:
                    event = Event.model_validate(raw)
                except Exception as exc:
                    if is_last:
                        logger.debug(
                            "replay_to_event_id: skipping invalid trailing event "
                            "(line %d): %s",
                            i + 1,
                            exc,
                        )
                        break
                    raise ValueError(
                        f"replay_to_event_id: cannot parse Event on interior line "
                        f"{i + 1}: {exc}"
                    ) from exc

                self._apply_write_only(conn, event)

                try:
                    seq = int(event.id[1:])
                    if seq > last_event_id:
                        last_event_id = seq
                except (ValueError, IndexError):
                    pass

                if event.id == stop_after_event_id:
                    found = True
                    break

            if not found:
                raise ValueError(
                    f"replay_to_event_id: stop_after_event_id "
                    f"{stop_after_event_id!r} not found in {events_path}."
                )

            if last_event_id > 0:
                self._next_seq = last_event_id
        finally:
            self._replaying = False

    def _replay_to_event_id_git(
        self, events_path: str, stop_after_event_id: str
    ) -> None:
        """Bounded rebuild for ``events_storage: git`` — the git-mode half of
        :meth:`replay_to_event_id`.

        Mirrors :meth:`_replay_from_empty_git` exactly (dedupe by id,
        hybrid-logical-clock ordering, torn-trailing-line tolerance, seq
        assignment) but stops after the event whose id equals
        ``stop_after_event_id`` in HLC order. Raises ``ValueError`` if that id
        is absent from the log.
        """
        self.close()
        for suffix in ("", "-wal", "-shm"):
            path = self._db_path + suffix
            if os.path.exists(path):
                os.remove(path)

        self._replaying = True
        try:
            self.initialize()

            if not os.path.exists(events_path):
                raise ValueError(
                    f"replay_to_event_id: events.jsonl does not exist but "
                    f"stop_after_event_id {stop_after_event_id!r} was requested."
                )

            conn = self._require_conn()

            with open(events_path, encoding="utf-8") as fh:
                lines = fh.readlines()

            events_by_id: dict[str, Event] = {}
            for i, raw_line in enumerate(lines):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                is_last = i == len(lines) - 1
                try:
                    raw: dict[str, Any] = json.loads(stripped)
                    event = Event.model_validate(raw)
                except Exception as exc:
                    if is_last:
                        logger.debug(
                            "git bounded replay: skipping torn trailing line "
                            "(line %d): %s",
                            i + 1,
                            exc,
                        )
                        break
                    raise ValueError(
                        f"git bounded replay: malformed event on interior line "
                        f"{i + 1}: {exc}"
                    ) from exc
                if event.id in events_by_id:
                    logger.debug(
                        "git bounded replay: duplicate event %s skipped", event.id
                    )
                    continue
                events_by_id[event.id] = event

            if stop_after_event_id not in events_by_id:
                raise ValueError(
                    f"replay_to_event_id: stop_after_event_id "
                    f"{stop_after_event_id!r} not found in {events_path}."
                )

            ordered = sorted(
                events_by_id.values(),
                key=lambda e: (e.lamport or 0, e.timestamp, e.id),
            )

            max_lamport = 0
            for seq, event in enumerate(ordered, start=1):
                self._apply_write_only(conn, event, seq=seq)
                if event.lamport is not None and event.lamport > max_lamport:
                    max_lamport = event.lamport
                if event.id == stop_after_event_id:
                    break
            self._max_lamport = max_lamport
        finally:
            self._replaying = False

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def get_task(self, task_id: str) -> Task | None:
        """Return the Task with the given ID, or None if not found."""
        conn = self._require_conn()
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_task(row, conn)

    def list_tasks(
        self,
        *,
        status: str | None = None,
        feature_id: str | None = None,
        task_type: str | None = None,
        prd_id: str | None = None,
    ) -> list[Task]:
        """Return tasks, optionally filtered by status, feature_id, task_type, prd_id.

        ``task_type`` (T015) pushes a ``task_type = ?`` clause to SQL so the
        ready queue / list surfaces can scope to feature / bugfix / refactor /
        modify work. Omitting it keeps the pre-T015 behaviour (all types).

        ``prd_id`` (T009) is the multi-PRD partition filter: ``None`` means all
        PRDs (the pre-T009 behaviour — byte-identical for existing callers),
        an explicit id adds ``prd_id = ?``.
        """
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[str] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if feature_id is not None:
            clauses.append("feature_id = ?")
            params.append(feature_id)
        if task_type is not None:
            clauses.append("task_type = ?")
            params.append(task_type)
        if prd_id is not None:
            clauses.append("prd_id = ?")
            params.append(prd_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(f"SELECT * FROM tasks {where}", params).fetchall()
        return [self._row_to_task(row, conn) for row in rows]

    def get_bundle(self, bundle_id: str) -> ExecutionBundle | None:
        """Return one execution bundle with ordered membership, or None."""
        conn = self._require_conn()
        row = conn.execute(
            "SELECT * FROM execution_bundles WHERE id = ?", (bundle_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_bundle(row, conn)

    def list_bundles(
        self, *, prd_id: str | None = None, status: str | None = None
    ) -> list[ExecutionBundle]:
        """Return execution bundles in deterministic ID order."""
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[str] = []
        if prd_id is not None:
            clauses.append("prd_id = ?")
            params.append(prd_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM execution_bundles {where}ORDER BY id", params
        ).fetchall()
        return [self._row_to_bundle(row, conn) for row in rows]

    def get_bundle_claim(self, bundle_id: str) -> BundleClaim | None:
        """Return the public coordinator claim for a bundle, if present."""
        conn = self._require_conn()
        row = conn.execute(
            "SELECT * FROM bundle_claims WHERE bundle_id = ? "
            "ORDER BY (status = 'active') DESC, created_at DESC, id DESC LIMIT 1",
            (bundle_id,),
        ).fetchone()
        return self._row_to_bundle_claim(row) if row is not None else None

    def list_bundle_claims(self, *, status: str | None = None) -> list[BundleClaim]:
        conn = self._require_conn()
        if status is None:
            rows = conn.execute("SELECT * FROM bundle_claims ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bundle_claims WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        return [self._row_to_bundle_claim(row) for row in rows]

    def list_bundle_reviews(
        self, bundle_id: str, *, disposition_event_id: str | None = None
    ) -> list[BundleReviewVerdict]:
        conn = self._require_conn()
        if disposition_event_id is None:
            rows = conn.execute(
                "SELECT * FROM bundle_review_verdicts WHERE bundle_id = ? "
                "ORDER BY disposition_event_id, review_round, angle, reviewed_by, id",
                (bundle_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bundle_review_verdicts WHERE bundle_id = ? "
                "AND disposition_event_id = ? "
                "ORDER BY review_round, angle, reviewed_by, id",
                (bundle_id, disposition_event_id),
            ).fetchall()
        return [BundleReviewVerdict.model_validate(dict(row)) for row in rows]

    def get_claim(self, claim_id: str) -> Claim | None:
        """Return the Claim with the given ID, or None if not found."""
        conn = self._require_conn()
        row = conn.execute(
            "SELECT * FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_claim(row)

    def list_active_claims(self) -> list[Claim]:
        """Return all claims with status == 'active'."""
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT * FROM claims WHERE status = ?",
            (ClaimStatus.active,),
        ).fetchall()
        return [self._row_to_claim(row) for row in rows]

    def list_claims(self) -> list[Claim]:
        """Return ALL claims regardless of status, sorted by id ASC.

        Includes active, released, stale, and force_released claims.
        The id-based ordering is deterministic because claim IDs follow
        the same C-prefixed format (e.g. 'C001') assigned at claim creation
        and never mutate.
        """
        conn = self._require_conn()
        rows = conn.execute(
            # ORDER BY id ASC: lexical order matches numeric only while the
            # zero-padded claim/event id suffix stays within its digit width.
            "SELECT * FROM claims ORDER BY id ASC"
        ).fetchall()
        return [self._row_to_claim(row) for row in rows]

    def list_reviews(self) -> list[Review]:
        """Return all Review rows sorted by id ASC.

        Covers both prd.approved reviews (id = RV-E{n}) and task.applied
        reviews (id = RV-E{n}).  The id-based ordering is deterministic
        because review IDs are derived deterministically from event IDs
        inside their handlers.
        """
        conn = self._require_conn()
        rows = conn.execute(
            # ORDER BY id ASC: lexical order matches numeric only while the
            # zero-padded event/id suffix stays within its digit width.
            "SELECT id, target_kind, target_id, reviewed_by, decision, notes, created_at "
            "FROM reviews ORDER BY id ASC"
        ).fetchall()
        return [self._row_to_review(row) for row in rows]

    def list_task_review_decisions(self) -> list[tuple[str, str, str]]:
        """Return (task_id, decision, created_at_iso) for every task.applied
        review outcome (decision in 'accepted' / 'rejected'), most-recent first.

        Reads the raw reviews table rather than the Review model: the
        ``ReviewDecision`` enum covers the PRD/finish-gate vocabulary
        (approve/reject/needs_changes), whereas task.applied records the
        accepted/rejected acceptance outcomes the B49 accept-rate governor needs.
        """
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT target_id, decision, created_at FROM reviews "
            "WHERE target_kind = 'task' AND decision IN ('accepted', 'rejected') "
            "ORDER BY created_at DESC"
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def list_evidence(self) -> list[Evidence]:
        """Return all Evidence rows sorted by id ASC.

        The id-based ordering is deterministic because evidence IDs are
        assigned by callers before emitting evidence.submitted events and
        are stable across replay.
        """
        conn = self._require_conn()
        rows = conn.execute(
            # ORDER BY id ASC: lexical order matches numeric only while the
            # zero-padded event/id suffix stays within its digit width.
            "SELECT id, task_id, claim_id, commands_run, output_excerpt, "
            "files_changed, pr_url, commit_sha, screenshots, "
            "known_limitations, submitted_at, submitted_by, proofs, "
            "category "
            "FROM evidence ORDER BY id ASC"
        ).fetchall()
        return [self._row_to_evidence(row) for row in rows]

    def list_requirements(
        self, *, prd_id: str | None = None, include_superseded: bool = False
    ) -> list[Requirement]:
        """Return Requirement rows sorted by id ASC.

        The id-based ordering is deterministic because requirement IDs are
        assigned at prd.parsed time and never mutate.

        T023 — by default this returns only the LIVE set (rows still current:
        ``revision_superseded IS NULL``). ``prd.revised`` is non-destructive — a
        superseded requirement keeps its row (stamped with the revision that
        retired it) so the lineage survives replay — but it is no longer part of
        the current requirement set, so it is filtered out by default. A
        single-parse PRD (the only shape a pre-T023 db ever had) has every row
        at ``revision_superseded IS NULL``, so this is byte-identical for those.

        ``include_superseded=True`` returns the FULL lineage (live + superseded)
        — used by ``serialize_state`` so the replay-equivalence oracle compares
        superseded-row lineage too. A superseded row carries a non-NULL
        ``revision_superseded`` on the returned model, so divergence in a
        retired row is observable in the snapshot.

        ``prd_id`` (T009) is the multi-PRD partition filter: ``None`` means all
        PRDs, an explicit id adds ``AND prd_id = ?``.
        """
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[str] = []
        if not include_superseded:
            clauses.append("revision_superseded IS NULL")
        if prd_id is not None:
            clauses.append("prd_id = ?")
            params.append(prd_id)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = conn.execute(
            "SELECT id, prd_section, text, source_paragraph, derived, "
            "revision_introduced, revision_superseded, prd_id "
            f"FROM requirements {where}ORDER BY id ASC",
            tuple(params),
        ).fetchall()
        return [self._row_to_requirement(row) for row in rows]

    def get_prd(self, prd_id: str | None = None) -> PRD | None:
        """Return a PRD, or None if not found.

        ``prd_id is None`` (the legacy no-arg call shape used by the 12 existing
        call sites) resolves the ``is_default = 1`` row — the single PRD on a
        single-PRD DB. ``ORDER BY id`` keeps the result deterministic if more
        than one default row ever existed (the ``ux_prds_default`` partial unique
        index makes that impossible per project, but the ORDER BY costs nothing
        and removes the ambiguity by construction).

        An explicit ``prd_id`` resolves ``WHERE id = ?`` — the partition lookup
        T008 adds for the multi-PRD phases.
        """
        conn = self._require_conn()
        if prd_id is None:
            row = conn.execute(
                "SELECT * FROM prds WHERE is_default = 1 ORDER BY id LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM prds WHERE id = ? LIMIT 1", (prd_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_prd(row)

    def list_prds(self) -> list[PRD]:
        """Return every PRD ordered by ``id`` ASC (deterministic for replay)."""
        conn = self._require_conn()
        rows = conn.execute("SELECT * FROM prds ORDER BY id").fetchall()
        return [self._row_to_prd(row) for row in rows]

    def default_prd_id(self) -> str | None:
        """Return the ``is_default = 1`` PRD's id, or None if no PRD exists."""
        conn = self._require_conn()
        row = conn.execute(
            "SELECT id FROM prds WHERE is_default = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def get_prd_for_task(self, task: Task) -> PRD | None:
        """Return the PRD that OWNS ``task``, resolved via ``task.prd_id`` (T011).

        Reads ``task.prd_id`` directly (no ``Feature`` join) and resolves it with
        ``get_prd(prd_id)`` — a single-column partition lookup. When the task
        carries no ``prd_id`` it falls back to the default PRD via the plain
        ``get_prd()`` (``is_default = 1``) resolver. On a single-PRD DB every
        task's ``prd_id`` is the default id, so this returns the same PRD the
        legacy no-arg ``get_prd()`` did — behaviour is unchanged there.
        """
        if not task.prd_id:
            return self.get_prd()
        return self.get_prd(task.prd_id)

    def get_project(self) -> Project | None:
        """Return the Project record, or None if not initialised."""
        conn = self._require_conn()
        row = conn.execute("SELECT * FROM projects").fetchone()
        if row is None:
            return None
        return self._row_to_project(row)

    def get_schema_version(self) -> int:
        """Return the schema version stamped on this database.

        T007/B11: reads ``PRAGMA user_version`` — the per-DB schema version
        that ``initialize()`` stamps (and ``_check_schema_version`` migrates).
        For a healthy, initialized DB this equals
        :data:`anvil.state.schema.SCHEMA_VERSION`; exposing the on-disk
        value lets tooling detect a DB that has not yet been migrated.
        """
        conn = self._require_conn()
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def get_feature(self, feature_id: str) -> Feature | None:
        """Return the Feature with the given ID, or None if not found."""
        conn = self._require_conn()
        row = conn.execute(
            "SELECT id, title, description, status, requirements, tasks "
            "FROM features WHERE id = ?",
            (feature_id,),
        ).fetchone()
        if row is None:
            return None
        return Feature(
            id=row[0],
            title=row[1],
            description=row[2],
            status=row[3],
            requirements=json.loads(row[4] or "[]"),
            tasks=json.loads(row[5] or "[]"),
        )

    def list_features(self, *, prd_id: str | None = None) -> list[Feature]:
        """Return Feature rows ordered by ID — see Protocol docstring.

        ``prd_id`` (T009) is the multi-PRD partition filter: ``None`` means all
        PRDs (byte-identical to the pre-T009 call), an explicit id adds
        ``WHERE prd_id = ?``.
        """
        conn = self._require_conn()
        where = "WHERE prd_id = ? " if prd_id is not None else ""
        params: tuple[str, ...] = (prd_id,) if prd_id is not None else ()
        rows = conn.execute(
            "SELECT id, title, description, status, requirements, tasks "
            f"FROM features {where}ORDER BY id",
            params,
        ).fetchall()
        return [
            Feature(
                id=r[0],
                title=r[1],
                description=r[2],
                status=r[3],
                requirements=json.loads(r[4] or "[]"),
                tasks=json.loads(r[5] or "[]"),
            )
            for r in rows
        ]

    def list_conflict_groups(self) -> list[ConflictGroup]:
        """Return all persisted ConflictGroup rows ordered by ID (CL-4).

        Read-only. The ``conflict_groups`` table is populated by
        ``conflict_group.upserted`` events emitted during planning/inference.
        """
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT id, name, task_ids, reason FROM conflict_groups ORDER BY id"
        ).fetchall()
        return [
            ConflictGroup(
                id=r[0],
                name=r[1],
                task_ids=json.loads(r[2] or "[]"),
                reason=r[3],
            )
            for r in rows
        ]

    def list_events(
        self,
        *,
        target_id: str,
        target_kind: str | None = None,
        limit: int = 10,
    ) -> list[tuple[str, str]]:
        """Return recent events for target as (action, timestamp_iso) tuples, most-recent first."""
        conn = self._require_conn()
        if target_kind is not None:
            rows = conn.execute(
                "SELECT action, timestamp FROM events "
                "WHERE target_id = ? AND target_kind = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (target_id, target_kind, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT action, timestamp FROM events "
                "WHERE target_id = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (target_id, limit),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def latest_event_payload(
        self, target_id: str, action: str
    ) -> tuple[dict, str] | None:
        """Return the most recent *action* event's (payload, timestamp_iso)
        for ``target_id``, or None (retro-opps T012).

        ``list_events`` deliberately drops payloads, so the heartbeat-bus
        read-back (latest ``progress.noted`` phase per active claim) needs
        this one-row fetch. Ordered by rowid (insertion order) like
        ``first_event_id`` so it is scheme-agnostic. A malformed payload
        row returns None rather than raising — status surfaces are
        best-effort readers.
        """
        import json as _json

        conn = self._require_conn()
        row = conn.execute(
            "SELECT payload_json, timestamp FROM events "
            "WHERE target_id = ? AND action = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (target_id, action),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload, row[1]

    def first_event_id(self, target_id: str) -> str | None:
        """Return the id of the earliest-recorded event for ``target_id``.

        Ordered by ``rowid`` (insertion order) so it is robust across both the
        local (zero-padded ``E000001``) and git (hash-chained ``E-<hex>``)
        event-id schemes. Used to bind an ``AcceptanceProof`` to the event-log
        range it covers. Returns None if the target has no events.
        """
        conn = self._require_conn()
        row = conn.execute(
            "SELECT id FROM events WHERE target_id = ? ORDER BY rowid ASC LIMIT 1",
            (target_id,),
        ).fetchone()
        return row[0] if row else None

    def get_latest_evidence(self, task_id: str) -> Evidence | None:
        """Return the most recently submitted Evidence for task_id, or None."""
        conn = self._require_conn()
        try:
            row = conn.execute(
                "SELECT id, task_id, claim_id, commands_run, output_excerpt, "
                "files_changed, pr_url, commit_sha, screenshots, "
                "known_limitations, submitted_at, submitted_by, proofs, "
                "category "
                "FROM evidence "
                "WHERE task_id = ? "
                "ORDER BY submitted_at DESC "
                "LIMIT 1",
                (task_id,),
            ).fetchone()
        except Exception:  # noqa: BLE001
            return None
        if row is None:
            return None
        return self._row_to_evidence(row)

    # ------------------------------------------------------------------
    # Phase 8 — sync mapping query helpers
    # ------------------------------------------------------------------

    def get_sync_mapping(
        self,
        task_id: str,
        *,
        external_system: str | None = None,
    ) -> SyncMapping | None:
        """Return the SyncMapping for ``task_id``, or None if not mapped.

        If ``external_system`` is None, returns the first mapping by
        ``external_system`` ASC — kept for backward-compat single-provider
        callers. Multi-provider callers MUST pass ``external_system``
        explicitly to get a scoped lookup; otherwise which provider's
        mapping wins is ASC-sort-position-dependent and brittle.
        """
        conn = self._require_conn()
        base = (
            "SELECT task_id, external_system, external_id, external_url, "
            "last_synced_at, sync_state, conflict_resolution_strategy, "
            "provider_metadata_json, prd_id, entity_kind FROM sync_mappings "
            "WHERE task_id = ?"
        )
        if external_system is None:
            row = conn.execute(
                base + " ORDER BY external_system ASC LIMIT 1",
                (task_id,),
            ).fetchone()
        else:
            row = conn.execute(
                base + " AND external_system = ? LIMIT 1",
                (task_id, external_system),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_sync_mapping(row)

    def list_sync_mappings(
        self,
        external_system: str | None = None,
    ) -> list[SyncMapping]:
        """Return SyncMapping rows, optionally filtered by external_system.

        Sorted by (task_id, external_system) ASC for deterministic output —
        important for replay-equality tests and for any CLI rendering that
        relies on stable ordering across runs.
        """
        conn = self._require_conn()
        base = (
            "SELECT task_id, external_system, external_id, external_url, "
            "last_synced_at, sync_state, conflict_resolution_strategy, "
            "provider_metadata_json, prd_id, entity_kind FROM sync_mappings"
        )
        if external_system is None:
            rows = conn.execute(
                base + " ORDER BY task_id ASC, external_system ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                base + " WHERE external_system = ? "
                "ORDER BY task_id ASC, external_system ASC",
                (external_system,),
            ).fetchall()
        return [self._row_to_sync_mapping(r) for r in rows]

    def apply_sync_mapping(
        self,
        mapping: SyncMapping,
        *,
        actor: str = "system",
    ) -> Event:
        """Build a sync_mapping.upserted draft and dispatch via ``append()``.

        Convenience for callers that want to write a mapping without having to
        construct the EventDraft/payload boilerplate. ``append`` assigns the id
        from the log-authority counter.

        Serializes the mapping through the canonical
        :class:`SyncMappingUpsertedPayload` model EXPLICITLY — not via
        ``mapping.model_dump()`` — so a hypothetical extra field on
        ``SyncMapping`` that hasn't been added to the payload model fails
        fast at THIS call site with a ``ValidationError`` rather than
        surfacing as ``TransactionAborted`` inside the lock. (Wave 1 critic fix MF-2.)
        """
        # prd_id / entity_kind are exclude=True on SyncMapping, so they are NOT
        # in mapping.model_dump() — pass them explicitly. A task-kind mapping
        # whose prd_id was never stamped (None) writes the default partition, so
        # replay matches the v6->v7 migration backfill (every legacy mapping is
        # owned by the default PRD).
        payload = SyncMappingUpsertedPayload(
            task_id=mapping.task_id,
            external_system=str(mapping.external_system),
            external_id=mapping.external_id,
            external_url=mapping.external_url,
            last_synced_at=mapping.last_synced_at.isoformat(),
            sync_state=str(mapping.sync_state),
            conflict_resolution_strategy=str(mapping.conflict_resolution_strategy),
            provider_metadata=dict(mapping.provider_metadata),
            prd_id=mapping.prd_id if mapping.prd_id is not None else DEFAULT_PRD_ID,
            entity_kind=mapping.entity_kind,
        )
        draft = EventDraft(
            timestamp=self._clock.now(),
            actor=actor,
            action="sync_mapping.upserted",
            target_kind="task",
            target_id=mapping.task_id,
            payload_json=payload.model_dump(mode="json"),
        )
        result = self.append(draft)
        if result is None:  # pragma: no cover — idempotent no-op
            raise TransactionAborted(
                "apply_sync_mapping: append returned None (idempotent no-op); "
                "this is unexpected for sync_mapping.upserted."
            )
        return result

    # ------------------------------------------------------------------
    # Internal helpers — DDL & version
    # ------------------------------------------------------------------

    def _apply_ddl(self) -> None:
        """Execute the DDL script statement-by-statement.

        Crucially this does NOT stamp ``PRAGMA user_version`` (BUG 002): the
        stamp is deferred to ``_check_schema_version``, which sets it ONLY
        after the migration ladder has actually run to completion. Stamping
        here — before the ladder — is what let a torn v6→v7 migration leave the
        DB permanently at user_version=7 while the schema was still partial:
        the next open would early-return on ``on_disk == SCHEMA_VERSION`` and
        never retry the ladder.
        """
        conn = self._require_conn()
        # Split on semicolons; filter blanks and PRAGMA user_version (stamped by
        # _check_schema_version, never here).
        statements = [s.strip() for s in DDL.split(";") if s.strip()]
        non_version = [s for s in statements if "user_version" not in s.lower()]
        conn.execute("BEGIN")
        try:
            for stmt in non_version:
                if not stmt:
                    continue
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    # A new index that references a column the pending migration
                    # has not added yet (e.g. v7's idx_*_prd / ux_prds_default on
                    # the prd_id / is_default columns) fails against a pre-existing
                    # older-shaped table. The DDL runs BEFORE _check_schema_version,
                    # so on an UPGRADING DB swallow exactly that "no such column"
                    # case for CREATE INDEX statements — the matching migration step
                    # re-creates the index (IF NOT EXISTS) right after it ALTERs the
                    # column in. A fresh/current DB must NOT swallow it (its CREATE
                    # TABLE already has the column, so a miss is a real bug).
                    msg = str(e).lower()
                    # Strip leading SQL line-comments / blank lines so the keyword
                    # check sees the actual statement (DDL statements can carry a
                    # ``-- ...`` comment line before the verb).
                    body = "\n".join(
                        line
                        for line in stmt.splitlines()
                        if line.strip() and not line.strip().startswith("--")
                    ).lower()
                    is_index = body.startswith("create index") or body.startswith(
                        "create unique index"
                    )
                    # Swallow ONLY the legitimate pre-migration case: a CREATE INDEX
                    # on a v7 column (prd_id / is_default) that an older-shaped table
                    # has not been ALTERed to yet — the ladder re-creates the index
                    # IF NOT EXISTS right after. A typo'd / unknown column name still
                    # propagates on every DB, incl. fresh/current (review FIX 4).
                    after = msg.partition("no such column:")[2].strip().split()
                    missing_col = after[0] if after else ""
                    if is_index and missing_col in (
                        "prd_id",
                        "is_default",
                        "disposition_event_id",
                    ):
                        continue
                    raise
            conn.execute("COMMIT")
        except BaseException:
            # A non-swallowed DDL failure leaves the BEGIN open; roll it back so the
            # connection isn't left mid-transaction before propagating (review FIX 5).
            self._safe_rollback(conn)
            raise
        # NOTE: user_version is intentionally NOT stamped here — see the
        # docstring. _check_schema_version stamps it after a successful ladder.

    def _check_schema_version(self, *, pre_ddl_version: int | None = None) -> None:
        """Raise SchemaMismatch if on-disk version is incompatible with SCHEMA_VERSION.

        Auto-upgrade behaviour (Phase 8 SF-6, refined by P1-3; v4 added in
        v1.22.0 for git-backed events Phase A; v5 added by T015 for
        non-feature task types):

        - ``v0 / v1 → v5``: ``sync_mappings`` never existed pre-v2, so the
          IF NOT EXISTS DDL created the current-shaped table from scratch;
          only the v4 ``events.seq`` and v5 ``tasks.task_type`` columns must
          be retrofitted onto the pre-existing tables.
        - ``v2 → v5``: NOT purely additive. The v2 db has a real
          ``sync_mappings`` table that ``CREATE TABLE IF NOT EXISTS``
          cannot retroactively modify — we must explicitly ALTER it to
          add ``external_url``, ``provider_metadata_json``, and the v3
          UNIQUE(external_system, external_id) index. Pre-fix this branch
          was a no-op stamp; queries against the new columns raised
          ``OperationalError`` until the v3 ALTERs landed. The v4
          ``events.seq`` and v5 ``tasks.task_type`` ALTERs ride along.
        - ``v3 → v5``: retrofit the nullable ``events.seq`` (v4) and
          ``tasks.task_type`` (v5) columns. The legacy table's strict
          events id CHECK (``E[0-9]*``) is deliberately left in place: SQLite
          cannot ALTER a CHECK, local mode never writes a hash id, and the
          git-mode entry path (``migrate-events`` → projection rebuild)
          recreates the table from the current DDL with the widened CHECK.
        - ``v4 → v5``: purely additive — ``tasks`` gains
          ``task_type TEXT NOT NULL DEFAULT 'feature'``. The DEFAULT
          backfills every existing row to ``'feature'`` (the pre-v5
          meaning), so no data migration is required.

        ``pre_ddl_version`` carries the user_version that was on disk
        BEFORE ``_apply_ddl`` re-stamped it. Required for every upgrade
        path: without it we'd always observe the post-DDL stamp (always
        equal to SCHEMA_VERSION) and the migration branches would never
        fire.

        Future gaps (e.g. a v5 db opened by code that expects v6) still
        raise. See docs/migrations.md.
        """
        conn = self._require_conn()
        # Use the pre-DDL version when provided (initialize path); fall back
        # to whatever PRAGMA reports now (early-return path where DDL did
        # not run between captures).
        if pre_ddl_version is not None:
            on_disk = pre_ddl_version
        else:
            row = conn.execute("PRAGMA user_version").fetchone()
            on_disk = row[0] if row else 0
        if on_disk == SCHEMA_VERSION:
            return
        # De-literalized migration ladder (was a chain of literal per-version
        # branches gated on the SCHEMA_VERSION constant). ``_MIGRATIONS`` is
        # an ordered list of ``(from_version, migrate_fn)`` steps; each ``fn``
        # applies exactly the additive delta that takes the DB from
        # ``from_version`` to ``from_version + 1`` and is idempotent
        # (duplicate-column tolerant) so a crash mid-migrate re-runs cleanly.
        # We apply every step whose ``from_version >= on_disk`` in order, then
        # stamp ``user_version``. Adding a future v8 step is ONE new tuple —
        # no literal version comparison to update.
        #
        # An older DB that is already past a given step (e.g. a v3 DB whose DDL
        # already grew current-shaped tables) still runs that step's helpers:
        # they no-op via duplicate-column tolerance / IF NOT EXISTS, so running
        # the full ordered ladder from ``on_disk`` is always safe.
        if on_disk > SCHEMA_VERSION or on_disk < 0:
            raise SchemaMismatch(
                f"Database schema version {on_disk} does not match "
                f"expected version {SCHEMA_VERSION}. "
                "Run a migration or delete state.db to start fresh."
            )
        applied = False
        for from_version, migrate_fn in self._MIGRATIONS:
            if on_disk <= from_version < SCHEMA_VERSION:
                migrate_fn(self, conn)
                applied = True
        if not applied:
            # No ladder step covers this on_disk version (e.g. a gap between a
            # known floor and SCHEMA_VERSION) — fail loudly rather than stamp a
            # version we did not actually migrate to.
            raise SchemaMismatch(
                f"Database schema version {on_disk} does not match "
                f"expected version {SCHEMA_VERSION}. "
                "Run a migration or delete state.db to start fresh."
            )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ------------------------------------------------------------------
    # Migration ladder steps. Each ``_m_to_vN`` applies the additive delta
    # from vN-1 to vN. All are idempotent (duplicate-column tolerant /
    # IF NOT EXISTS) so re-running after a crash is a no-op.
    # ------------------------------------------------------------------

    def _m_to_v3(self, conn: sqlite3.Connection) -> None:
        """v2 → v3: the three sync_mappings additions the IF NOT EXISTS DDL
        cannot retroactively apply to a real (pre-existing) v2 table.

        On a v0/v1 DB (no pre-existing sync_mappings — DDL created the current
        shape) these ALTERs hit duplicate-column tolerance and no-op.
        """
        try:
            conn.execute("ALTER TABLE sync_mappings ADD COLUMN external_url TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
        try:
            conn.execute(
                "ALTER TABLE sync_mappings ADD COLUMN provider_metadata_json TEXT"
            )
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
        # SQLite cannot add a table-level UNIQUE via ALTER; a UNIQUE INDEX has
        # the same enforcement. IF NOT EXISTS makes it replay-safe.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_sync_mappings_external_unique "
            "ON sync_mappings (external_system, external_id)"
        )

    def _m_to_v4(self, conn: sqlite3.Connection) -> None:
        """v3 → v4: retrofit the nullable ``events.seq`` column."""
        self._ensure_events_seq_column(conn)

    def _m_to_v5(self, conn: sqlite3.Connection) -> None:
        """v4 → v5: retrofit ``tasks.task_type`` (DEFAULT 'feature')."""
        self._ensure_task_type_column(conn)

    def _m_to_v6(self, conn: sqlite3.Connection) -> None:
        """v5 → v6: retrofit ``evidence.proofs`` (DEFAULT '[]')."""
        self._ensure_evidence_proofs_column(conn)

    def _m_to_v7(self, conn: sqlite3.Connection) -> None:
        """v6 → v7: v0.3 multi-PRD persistence foundation.

        Steps, all idempotent / crash-safe:
        1. Rebuild ``prds`` (SQLite cannot ALTER a PRIMARY KEY; atomicity comes
           from the outer transaction — see "Atomicity (BUG 002)" below):
           CREATE ``prds_new`` with the single-column ``id`` PK + the new
           identity/release columns, INSERT-SELECT the existing single row as
           ``id='default'`` / ``is_default=1`` / ``created_at`` =
           COALESCE(last_reviewed_at, project.created_at), DROP old, RENAME.
           Guarded so a re-run after a torn rebuild (prds already migrated)
           is a no-op.
        2. ALTER requirements/features/tasks ADD COLUMN ``prd_id`` TEXT NOT NULL
           DEFAULT 'default' — the DEFAULT backfills every existing row in one
           statement. requirements also gains nullable revision lineage columns.
        3. ALTER sync_mappings ADD ``prd_id`` + ``entity_kind``; backfill
           ``prd_id`` by joining mapping.task_id -> task.prd_id (== 'default').
        4. CREATE the new indexes + ux_prds_default IF NOT EXISTS.

        The 'default' literal MUST equal ``DEFAULT_PRD_ID`` (models.py) or
        replay forks: a pre-v7 log replays into the same partition this
        migration mints.

        Atomicity (BUG 002): the connection runs in autocommit
        (``isolation_level=None``), so every statement would otherwise commit
        individually — a crash part-way (disk full, lock, SIGKILL) would leave
        a partial-v7 schema on disk. We wrap the ENTIRE body in one explicit
        ``BEGIN IMMEDIATE … COMMIT`` (ROLLBACK on any exception). SQLite DDL is
        transactional, so a torn migration rolls back every change atomically
        and ``user_version`` stays at the pre-migrate value (6) for a clean
        retry on the next open. The body remains idempotent (duplicate-column
        tolerant / IF NOT EXISTS) so a completed-then-rerun ladder no-ops.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._m_to_v7_body(conn)
        except BaseException:
            # BaseException, not Exception: a SIGKILL surfaces as
            # KeyboardInterrupt; we still want the in-flight transaction rolled
            # back so nothing partial lands on disk. Use _safe_rollback so a
            # ROLLBACK against an already-auto-aborted transaction (SQLITE_FULL/
            # IOERR) does not mask the original migration failure (review FIX 2).
            self._safe_rollback(conn)
            raise
        conn.execute("COMMIT")

    def _m_to_v7_body(self, conn: sqlite3.Connection) -> None:
        """The v6→v7 migration statements, run inside the explicit transaction
        opened by ``_m_to_v7``. Kept idempotent / crash-safe."""
        # ---- 1. Rebuild prds (PK change requires table rebuild) ----
        # Detect whether the rebuild already ran (idempotency after a crash).
        prds_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(prds)").fetchall()
        }
        if "is_default" not in prds_cols:
            # project.created_at is the fallback timestamp for the default PRD.
            proj_row = conn.execute(
                "SELECT created_at FROM projects"
            ).fetchone()
            project_created_at = proj_row[0] if proj_row else None
            # Crash-safety: a previous migration that died AFTER
            # ``CREATE TABLE prds_new`` but BEFORE the ``DROP TABLE prds`` /
            # ``RENAME`` leaves a stray ``prds_new`` behind while ``prds``
            # still lacks ``is_default`` — so the guard above does not catch
            # it and the CREATE below would raise "table prds_new already
            # exists". Drop any stale ``prds_new`` first so a re-run after a
            # torn rebuild heals cleanly instead of crashing. (With the
            # outer transaction a torn run rolls back fully, but the guard is
            # retained as defence in depth.)
            conn.execute("DROP TABLE IF EXISTS prds_new")
            conn.execute(
                """
                CREATE TABLE prds_new (
                    id                  TEXT PRIMARY KEY DEFAULT 'default',
                    project_id          TEXT NOT NULL DEFAULT '',
                    title               TEXT NOT NULL DEFAULT '',
                    status              TEXT NOT NULL DEFAULT 'draft',
                    summary             TEXT NOT NULL DEFAULT '',
                    goals               TEXT NOT NULL DEFAULT '[]',
                    non_goals           TEXT NOT NULL DEFAULT '[]',
                    requirements        TEXT NOT NULL DEFAULT '[]',
                    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
                    risks               TEXT NOT NULL DEFAULT '[]',
                    open_questions      TEXT NOT NULL DEFAULT '[]',
                    last_reviewed_at    TEXT,
                    last_reviewed_by    TEXT,
                    target_version      TEXT,
                    target_tag          TEXT,
                    is_default          INTEGER NOT NULL DEFAULT 0,
                    created_at          TEXT,
                    updated_at          TEXT
                )
                """
            )
            # Carry the single existing PRD row forward as the default PRD.
            conn.execute(
                """
                INSERT INTO prds_new (
                    id, project_id, title, status, summary, goals, non_goals,
                    requirements, acceptance_criteria, risks, open_questions,
                    last_reviewed_at, last_reviewed_by, target_version,
                    target_tag, is_default, created_at, updated_at
                )
                SELECT
                    'default', project_id, '', status, summary, goals,
                    non_goals, requirements, acceptance_criteria, risks,
                    open_questions, last_reviewed_at, last_reviewed_by,
                    NULL, NULL, 1,
                    COALESCE(last_reviewed_at, ?),
                    COALESCE(last_reviewed_at, ?)
                FROM prds
                """,
                (project_created_at, project_created_at),
            )
            conn.execute("DROP TABLE prds")
            conn.execute("ALTER TABLE prds_new RENAME TO prds")

        # NOTE: the per-PRD ``revision`` column is NOT added here — it is a v8
        # change (``_m_to_v8``). v7 already shipped without it, so a DB stamped
        # at v7 must grow it via the v8 ladder step, not a retroactive edit to
        # the v7 shape.

        # ---- 2. Additive prd_id + revision lineage on entity tables ----
        for table in ("requirements", "features", "tasks"):
            try:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN "
                    "prd_id TEXT NOT NULL DEFAULT 'default'"
                )
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        for col in ("revision_introduced", "revision_superseded"):
            try:
                conn.execute(
                    f"ALTER TABLE requirements ADD COLUMN {col} INTEGER"
                )
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

        # ---- 3. sync_mappings partition columns ----
        try:
            conn.execute("ALTER TABLE sync_mappings ADD COLUMN prd_id TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
        try:
            conn.execute(
                "ALTER TABLE sync_mappings ADD COLUMN "
                "entity_kind TEXT NOT NULL DEFAULT 'task'"
            )
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
        # Backfill prd_id from the owning task (every task is 'default' now).
        conn.execute(
            "UPDATE sync_mappings SET prd_id = ("
            "  SELECT t.prd_id FROM tasks t WHERE t.id = sync_mappings.task_id"
            ") WHERE prd_id IS NULL"
        )

        # ---- 4. New indexes + partial unique default index ----
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_requirements_prd "
            "ON requirements (prd_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_features_prd ON features (prd_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_prd_status "
            "ON tasks (prd_id, status)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_prds_default "
            "ON prds (project_id) WHERE is_default = 1"
        )

    def _m_to_v8(self, conn: sqlite3.Connection) -> None:
        """v7 → v8: per-PRD ``revision`` counter on ``prds`` (T023).

        Purely additive: ALTER ADD COLUMN ``revision INTEGER NOT NULL
        DEFAULT 1`` backfills every existing PRD row to revision 1 (the correct
        pre-revision meaning). Duplicate-column tolerant so a re-run after a
        crash (or a fresh v6→v8 DB whose v7 rebuild already grew the column from
        the current DDL) no-ops.

        This is a SEPARATE step from ``_m_to_v7`` on purpose: v7 shipped without
        ``revision`` (PR #80), so a DB already stamped at v7 early-returns on
        ``on_disk == SCHEMA_VERSION`` and never re-enters the v7 body. Bumping
        SCHEMA_VERSION to 8 and adding this step is what re-arms the ladder for
        those DBs.
        """
        try:
            conn.execute(
                "ALTER TABLE prds ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            )
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    def _m_to_v9(self, conn: sqlite3.Connection) -> None:
        """v8 → v9: evidence contracts (issue #153 / evidence-contracts PRD).

        Purely additive, duplicate-column tolerant like every ladder step:

        - ``tasks.claims TEXT NOT NULL DEFAULT '[]'`` — the task's named
          TaskClaims; ``'[]'`` backfills every existing row to "no claims",
          the correct pre-feature meaning.
        - ``evidence.category TEXT NOT NULL DEFAULT 'completion'`` — the
          evidence role; ``'completion'`` backfills the historical meaning
          (all pre-feature evidence was completion evidence).
        """
        for ddl in (
            "ALTER TABLE tasks ADD COLUMN claims TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE evidence ADD COLUMN "
            "category TEXT NOT NULL DEFAULT 'completion'",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    # Ordered migration ladder: (from_version, bound-method). Applied while
    # ``on_disk <= from_version < SCHEMA_VERSION``. Append one tuple per future
    # schema bump; never edit a literal version comparison again.
    def _m_to_v10(self, conn: sqlite3.Connection) -> None:
        """v9 -> v10: distinct-actor fail-fast (retro corpus, concurrency theme).

        Purely additive, duplicate-column tolerant like every ladder step:

        - ``claims.session_id TEXT`` (nullable) — the claiming loop's session
          discriminator, recorded independently of the actor string. NULL
          backfills every existing row to "session unknown", the correct
          pre-feature meaning (the fail-fast skips NULL-session claims).
        """
        try:
            conn.execute("ALTER TABLE claims ADD COLUMN session_id TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    def _m_to_v11(self, conn: sqlite3.Connection) -> None:
        """v10 -> v11: first-class execution bundle persistence.

        The current DDL runs before the migration ladder, so these statements
        are intentionally idempotent. Keeping the ladder step is load-bearing:
        it is what permits a genuine v10 database to be stamped v11 only after
        every bundle table and index exists.
        """
        statements = (
            """CREATE TABLE IF NOT EXISTS execution_bundles (
                id TEXT PRIMARY KEY,
                creation_event_id TEXT NOT NULL UNIQUE,
                prd_id TEXT NOT NULL REFERENCES prds(id) ON DELETE RESTRICT,
                coordinator TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                review_disposition_event_id TEXT,
                superseded_by TEXT REFERENCES execution_bundles(id) ON DELETE RESTRICT,
                last_result_at TEXT,
                branch TEXT,
                worktree_path TEXT,
                review_policy TEXT NOT NULL DEFAULT '{}',
                throughput_budget TEXT NOT NULL DEFAULT '{}',
                delegated_agents TEXT NOT NULL DEFAULT '[]',
                checkpoint TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_execution_bundles_status "
            "ON execution_bundles (status)",
            "CREATE INDEX IF NOT EXISTS idx_execution_bundles_prd_status "
            "ON execution_bundles (prd_id, status)",
            """CREATE TABLE IF NOT EXISTS execution_bundle_members (
                bundle_id TEXT NOT NULL REFERENCES execution_bundles(id) ON DELETE RESTRICT,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
                position INTEGER NOT NULL CHECK (position >= 0),
                PRIMARY KEY (bundle_id, task_id),
                UNIQUE (bundle_id, position)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_execution_bundle_members_task "
            "ON execution_bundle_members (task_id)",
            """CREATE TABLE IF NOT EXISTS bundle_review_verdicts (
                id TEXT PRIMARY KEY,
                bundle_id TEXT NOT NULL
                    REFERENCES execution_bundles(id) ON DELETE RESTRICT,
                creation_event_id TEXT NOT NULL,
                disposition_event_id TEXT NOT NULL,
                review_round INTEGER NOT NULL CHECK (review_round >= 1),
                angle TEXT NOT NULL,
                reviewed_by TEXT NOT NULL,
                decision TEXT NOT NULL,
                notes TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (bundle_id, creation_event_id, disposition_event_id,
                        review_round, angle, reviewed_by)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_bundle_review_verdicts_round "
            "ON bundle_review_verdicts "
            "(bundle_id, creation_event_id, disposition_event_id, review_round)",
            """CREATE TABLE IF NOT EXISTS claim_replay_lineages (
                claim_id TEXT PRIMARY KEY REFERENCES claims(id) ON DELETE RESTRICT,
                creation_fingerprint TEXT NOT NULL,
                collision_detected INTEGER NOT NULL DEFAULT 0
                    CHECK (collision_detected IN (0, 1))
            )""",
        )
        for ddl in statements:
            conn.execute(ddl)

        # Genuine v10 projections already contain claims. Seed their immutable
        # creation fingerprint from the earliest stored creation event before
        # forward catch-up can encounter a divergent duplicate. If historical
        # event data is unavailable or malformed, use an explicit unknown
        # sentinel: any future claim.created payload will differ and therefore
        # fail closed by marking the lineage collided.
        claim_rows = conn.execute("SELECT id FROM claims ORDER BY id").fetchall()
        for claim_row in claim_rows:
            claim_id = claim_row[0]
            event_row = conn.execute(
                "SELECT payload_json FROM events "
                "WHERE action = 'claim.created' AND target_kind = 'claim' "
                "AND target_id = ? ORDER BY rowid LIMIT 1",
                (claim_id,),
            ).fetchone()
            fingerprint = f"legacy-unknown:{claim_id}"
            if event_row is not None:
                try:
                    creation_payload = ClaimCreatedPayload.model_validate(
                        json.loads(event_row[0])
                    )
                    fingerprint = self._claim_creation_fingerprint(creation_payload)
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            conn.execute(
                "INSERT OR IGNORE INTO claim_replay_lineages "
                "(claim_id, creation_fingerprint) VALUES (?, ?)",
                (claim_id, fingerprint),
            )

    def _m_to_v12(self, conn: sqlite3.Connection) -> None:
        """v11 -> v12: coordinator bundle claims and member authorizations."""
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bundle_claims (
                id TEXT PRIMARY KEY,
                bundle_id TEXT NOT NULL
                    REFERENCES execution_bundles(id) ON DELETE RESTRICT,
                claimed_by TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                branch TEXT,
                worktree_path TEXT,
                session_id TEXT,
                expected_files TEXT NOT NULL DEFAULT '[]',
                member_claim_ids TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                last_heartbeat_at TEXT NOT NULL,
                released_at TEXT,
                release_reason TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bundle_claims_status "
            "ON bundle_claims (status)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_bundle_claims_active_bundle "
            "ON bundle_claims (bundle_id) WHERE status = 'active'"
        )
        try:
            conn.execute(
                "ALTER TABLE claims ADD COLUMN bundle_claim_id TEXT "
                "REFERENCES bundle_claims(id) ON DELETE RESTRICT"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    def _m_to_v13(self, conn: sqlite3.Connection) -> None:
        """v12 -> v13: bind reviews to one implemented disposition event."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._m_to_v13_body(conn)
        except BaseException:
            self._safe_rollback(conn)
            raise
        conn.execute("COMMIT")

    def _m_to_v13_body(self, conn: sqlite3.Connection) -> None:
        """Run the v13 table rebuild atomically and recover an old torn rename."""
        bundle_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(execution_bundles)")
        }
        if "review_disposition_event_id" not in bundle_columns:
            conn.execute(
                "ALTER TABLE execution_bundles "
                "ADD COLUMN review_disposition_event_id TEXT"
            )

        backup_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'bundle_review_verdicts_v12'"
        ).fetchone() is not None
        review_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(bundle_review_verdicts)")
        }
        if review_columns and "disposition_event_id" not in review_columns:
            conn.execute("ALTER TABLE bundle_review_verdicts RENAME TO bundle_review_verdicts_v12")
            backup_exists = True
            review_columns = set()
        if not review_columns:
            conn.execute(
                """CREATE TABLE bundle_review_verdicts (
                    id TEXT PRIMARY KEY,
                    bundle_id TEXT NOT NULL
                        REFERENCES execution_bundles(id) ON DELETE RESTRICT,
                    creation_event_id TEXT NOT NULL,
                    disposition_event_id TEXT NOT NULL,
                    review_round INTEGER NOT NULL CHECK (review_round >= 1),
                    angle TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    notes TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (bundle_id, creation_event_id, disposition_event_id,
                            review_round, angle, reviewed_by)
                )"""
            )
        if backup_exists:
            conn.execute(
                "INSERT OR IGNORE INTO bundle_review_verdicts "
                "(id, bundle_id, creation_event_id, disposition_event_id, "
                "review_round, angle, reviewed_by, decision, notes, created_at) "
                "SELECT id, bundle_id, creation_event_id, 'legacy-unbound', "
                "review_round, angle, reviewed_by, decision, notes, created_at "
                "FROM bundle_review_verdicts_v12"
            )
            conn.execute("DROP TABLE bundle_review_verdicts_v12")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bundle_review_verdicts_round "
            "ON bundle_review_verdicts "
            "(bundle_id, creation_event_id, disposition_event_id, review_round)"
        )

        latest_by_bundle: dict[str, str] = {}
        rows = conn.execute(
            "SELECT id, target_id, payload_json FROM events "
            "WHERE action = 'bundle.status_changed' AND target_kind = 'bundle' "
            "ORDER BY rowid"
        ).fetchall()
        for event_id, bundle_id, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("to") == BundleStatus.implemented_unreviewed.value:
                latest_by_bundle[bundle_id] = event_id
        for bundle_id, event_id in latest_by_bundle.items():
            conn.execute(
                "UPDATE execution_bundles SET review_disposition_event_id = ? "
                "WHERE id = ?",
                (event_id, bundle_id),
            )

    def _m_to_v14(self, conn: sqlite3.Connection) -> None:
        """v13 -> v14: named, history-preserving bundle supersession."""
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(execution_bundles)")
        }
        if "superseded_by" not in columns:
            conn.execute(
                "ALTER TABLE execution_bundles ADD COLUMN superseded_by TEXT "
                "REFERENCES execution_bundles(id) ON DELETE RESTRICT"
            )

    def _m_to_v15(self, conn: sqlite3.Connection) -> None:
        """v14 -> v15: authoritative applied lifecycle-result timestamp."""
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(execution_bundles)")
        }
        if "last_result_at" not in columns:
            conn.execute("ALTER TABLE execution_bundles ADD COLUMN last_result_at TEXT")
        # v14 did not retain the exact result transition separately. Use the
        # projection's own last-applied mutation time as a conservative
        # baseline; never inspect raw event payloads, which may include replay
        # no-ops. New v15 transitions record the exact result time.
        conn.execute(
            "UPDATE execution_bundles SET last_result_at = updated_at "
            "WHERE last_result_at IS NULL AND status IN "
            "('reviewed_unintegrated', 'integrated', 'merged', 'completed')"
        )

    def _m_to_v16(self, conn: sqlite3.Connection) -> None:
        """v15 -> v16: persist typed PRD assumptions as canonical JSON."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(prds)")}
        if "assumptions" not in columns:
            conn.execute(
                "ALTER TABLE prds ADD COLUMN assumptions TEXT NOT NULL DEFAULT '[]'"
            )

    _MIGRATIONS: list[tuple[int, Any]] = [
        (2, _m_to_v3),
        (3, _m_to_v4),
        (4, _m_to_v5),
        (5, _m_to_v6),
        (6, _m_to_v7),
        (7, _m_to_v8),
        (8, _m_to_v9),
        (9, _m_to_v10),
        (10, _m_to_v11),
        (11, _m_to_v12),
        (12, _m_to_v13),
        (13, _m_to_v14),
        (14, _m_to_v15),
        (15, _m_to_v16),
    ]

    @staticmethod
    def _ensure_events_seq_column(conn: sqlite3.Connection) -> None:
        """ALTER events ADD COLUMN seq if it is missing (v4, idempotent).

        Wrapped in duplicate-column tolerance for the same reason as the
        v2→v3 ALTERs: re-running the migration after a crash must not fail.
        """
        try:
            conn.execute("ALTER TABLE events ADD COLUMN seq INTEGER")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    @staticmethod
    def _ensure_task_type_column(conn: sqlite3.Connection) -> None:
        """ALTER tasks ADD COLUMN task_type if it is missing (v5, idempotent).

        The DEFAULT 'feature' backfills every pre-v5 row to the value that
        matches its original meaning, so the column is purely additive.
        Wrapped in duplicate-column tolerance so re-running the migration
        after a crash (or a v4-DDL table that already grew the column from
        the current ``CREATE TABLE``) is a silent no-op.
        """
        try:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN "
                "task_type TEXT NOT NULL DEFAULT 'feature'"
            )
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    @staticmethod
    def _ensure_evidence_proofs_column(conn: sqlite3.Connection) -> None:
        """ALTER evidence ADD COLUMN proofs if missing (v6, idempotent).

        SL-3 / B48 typed proofs. The DEFAULT '[]' backfills every pre-v6 row to
        "no typed proofs," which is the correct pre-SL-3 meaning, so the column
        is purely additive. Wrapped in duplicate-column tolerance so re-running
        the migration after a crash (or a v5-DDL table that already grew the
        column from the current ``CREATE TABLE``) is a silent no-op.
        """
        try:
            conn.execute(
                "ALTER TABLE evidence ADD COLUMN "
                "proofs TEXT NOT NULL DEFAULT '[]'"
            )
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    def _require_conn(self) -> sqlite3.Connection:
        """Return the open connection or raise if not initialised."""
        if self._conn is None:
            raise RuntimeError(
                "SqliteBackend.initialize() must be called before any query or mutation."
            )
        return self._conn

    # ------------------------------------------------------------------
    # Internal helpers — SL1-RR-1 log-authority / lock / audit
    # ------------------------------------------------------------------

    @contextmanager
    def _append_lock(self) -> Iterator[None]:
        """Serialize appends with a threading.Lock + flock on events.jsonl.

        The threading.Lock serializes concurrent appends from different threads
        in the same process (e.g., MCP server + CLI in one process). The flock
        on ``events.jsonl`` serializes concurrent appends from different processes
        (e.g., two CLI invocations). Together they guarantee no id collision and
        no lost events.

        The flock uses a 5-second contention timeout matching SQLite's
        ``busy_timeout``; contention beyond it raises ``StateLocked``. Retries
        follow the jittered exponential schedule of ``_flock_backoff_delays``
        so a coordinated wave of claimants does not poll in lockstep.
        """
        with self._proc_lock:
            # Ensure the log file exists before we try to flock it.
            log_path = self._events_path
            if not os.path.exists(log_path):
                open(log_path, "a", encoding="utf-8").close()  # noqa: WPS515
            # Binary handle: the Windows lock path os.lseek()s this fd to a
            # sentinel offset, which a text handle would refuse. The handle is a
            # pure lock token — only its fileno() is used, never read/written.
            with open(log_path, "ab") as _lock_fh:
                # Try a non-blocking exclusive lock first; if contended, retry
                # with jittered exponential backoff until the 5 s budget is
                # spent. The deadline is measured on the monotonic clock, NOT
                # self._clock: a wall-clock NTP step mid-contention would
                # silently stretch or shorten the timeout.
                delays = _flock_backoff_delays()
                deadline = self._monotonic_fn() + _FLOCK_TIMEOUT_S
                while True:
                    try:
                        # No-op when neither fcntl nor msvcrt exists, so the loop
                        # breaks immediately and we rely on self._proc_lock alone.
                        _append_lock_acquire_nb(_lock_fh)
                        break
                    except OSError as lock_exc:
                        remaining = deadline - self._monotonic_fn()
                        if remaining <= 0:
                            raise StateLocked(
                                "append: flock contention on events.jsonl exceeded "
                                f"{_FLOCK_TIMEOUT_S:g} s timeout"
                            ) from lock_exc
                        # Clamp so the final sleep cannot overshoot the budget.
                        self._sleep_fn(min(next(delays), remaining))
                try:
                    yield
                finally:
                    _append_lock_release(_lock_fh)

    @contextmanager
    def claim_operation_lock(self) -> Iterator[None]:
        """Serialize one logical claim operation on this backend instance.

        The append path already serializes writes, but claim also performs
        pre-append reads (task status, PRD gate, conflict checks). A single
        sqlite3 connection is not safe for overlapping reads from many threads,
        even with ``check_same_thread=False``. This lock lets ClaimManager make
        those reads and the eventual append one same-process critical section.
        Cross-process atomicity still comes from the events.jsonl flock and
        SQLite write transaction inside append().
        """
        with self._proc_lock:
            yield

    def _read_tail_window(self) -> list[bytes]:
        """Return candidate raw lines from the end of events.jsonl, oldest first.

        Shared by ``_scan_tail_id`` (local mode) and ``_scan_tail_envelope``
        (git mode). Reads a window from the end of the file — O(window), not
        O(file size). Returns ``[]`` when the file is missing or empty.

        Large-line tolerance (SHOULD FIX 1): starts with a 4096-byte window
        and doubles until the window contains at least one newline separator
        (ensuring at least one complete prior line to fall back to) or spans
        the entire file, so events with large ``prd.parsed`` /
        ``task.expanded`` payloads are handled correctly.
        """
        log_path = self._events_path
        if not os.path.exists(log_path):
            return []
        file_size = os.path.getsize(log_path)
        if file_size == 0:
            return []

        chunk_size = min(4096, file_size)
        with open(log_path, "rb") as fh:
            while True:
                fh.seek(-chunk_size, 2)  # 2 = os.SEEK_END
                chunk = fh.read(chunk_size)
                # Strip trailing whitespace/newlines to ignore blank trailing lines.
                stripped = chunk.rstrip(b"\n\r ")
                # Check whether the stripped chunk contains at least one newline
                # (meaning we have at least one complete prior line to fall back to).
                if b"\n" in stripped or chunk_size >= file_size:
                    break
                # No newline found and we haven't read the full file yet — double.
                chunk_size = min(chunk_size * 2, file_size)

        return stripped.split(b"\n")

    def _scan_tail_id(self) -> int:
        """Return the numeric part of the last event id in events.jsonl.

        Local mode. If the file does not exist or is empty, returns 0.

        Torn-line tolerance (MUST FIX — SL1-RR-1 critic issue 1 + SHOULD FIX 1):
        The final line of the file may be a torn partial write (crash mid-append).
        A torn line will fail JSON parsing or carry no valid E###### id. We walk
        backward through the candidate lines in the tail window and return the
        *first* line that carries a valid E###### id — skipping the torn/idless
        trailing line and falling back to the previous complete line.
        """
        # Walk from the last line backwards; return the first valid E###### id found.
        # This skips a torn or id-less trailing line and falls back to the previous
        # complete line, matching replay_from_empty's torn-trailing-line tolerance.
        for candidate in reversed(self._read_tail_window()):
            candidate = candidate.strip()
            if not candidate:
                continue
            try:
                raw = json.loads(candidate.decode("utf-8"))
                event_id: str = raw.get("id", "")
                if event_id.startswith("E") and event_id[1:].isdigit():
                    return int(event_id[1:])
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                continue

        return 0

    def _scan_tail_envelope(self) -> tuple[str | None, int]:
        """Return (event_id, lamport) of the last valid log line — git mode.

        The chain parent for the next append is the last event in FILE order,
        not HLC order: the spec defines the parent as "the previous event as
        seen by the writer", and under the flock the file tail is exactly
        that. Torn or id-less trailing lines are skipped with fallback to the
        previous complete line, mirroring ``_scan_tail_id``. Returns
        ``(None, 0)`` for an empty/missing log — the first event is the chain
        root and has no parent.
        """
        for candidate in reversed(self._read_tail_window()):
            candidate = candidate.strip()
            if not candidate:
                continue
            try:
                raw = json.loads(candidate.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            event_id = raw.get("id")
            if not isinstance(event_id, str) or not event_id:
                continue
            lamport_raw = raw.get("lamport")
            # bool is an int subclass — `lamport: true` must not count as 1.
            if isinstance(lamport_raw, int) and not isinstance(lamport_raw, bool):
                return event_id, lamport_raw
            return event_id, 0
        return None, 0

    def _scan_log_ids_and_lamport(self) -> tuple[set[str], int]:
        """Full-log scan: every event id + the Lamport high-water mark (git mode).

        O(file size) by design — git mode needs the full id set for the
        convergence check (a merge can splice events into the interior), and
        the same pass seeds ``_max_lamport``. Tolerates the torn trailing
        line exactly like replay; an interior malformed line raises because
        that is corruption, not a torn write.
        """
        ids: set[str] = set()
        max_lamport = 0
        if not os.path.exists(self._events_path):
            return ids, max_lamport
        with open(self._events_path, encoding="utf-8") as fh:
            lines = fh.readlines()
        for i, raw_line in enumerate(lines):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                raw: dict[str, Any] = json.loads(stripped)
            except json.JSONDecodeError as exc:
                if i == len(lines) - 1:
                    continue  # torn trailing line — same tolerance as replay
                raise ValueError(
                    f"events log scan: malformed JSON on interior line {i + 1}: {exc}"
                ) from exc
            event_id = raw.get("id")
            if isinstance(event_id, str) and event_id:
                ids.add(event_id)
            lamport_raw = raw.get("lamport")
            if (
                isinstance(lamport_raw, int)
                and not isinstance(lamport_raw, bool)
                and lamport_raw > max_lamport
            ):
                max_lamport = lamport_raw
        return ids, max_lamport

    def _git_converge_projection(self) -> None:
        """Heal the projection in git mode: full rebuild when log/table diverge.

        Loads every event id in the log (tolerating the torn trailing line),
        seeds the in-memory Lamport high-water mark, then compares the id SET
        against the events table. On any difference — log ahead (fresh clone,
        crash between log append and COMMIT), log rewritten (migrate-events),
        or merged-in interior events — the projection is rebuilt from scratch
        via the order-tolerant git replay. Set comparison (not max/count) is
        the only sound convergence test once ``merge=union`` can splice
        events into the interior of the file.
        """
        conn = self._require_conn()
        log_ids, max_lamport = self._scan_log_ids_and_lamport()
        self._max_lamport = max_lamport
        table_ids = {
            row[0] for row in conn.execute("SELECT id FROM events").fetchall()
        }
        if log_ids != table_ids:
            self.replay_from_empty(self._events_path)

    def _serialize_event_line(self, event: Event) -> str:
        """Serialize *event* to its newline-terminated JSONL line.

        Local mode omits the git-mode envelope fields (``parent_event_id``,
        ``lamport`` — always None here) so the line bytes stay identical to
        every pre-1.22.0 log: existing fixtures, goldens, and on-disk logs
        must not churn. Git mode always emits both keys — a uniform line
        shape per mode beats per-line optionality (the chain root's parent is
        an explicit ``null``).
        """
        if self._events_storage == "git":
            return event.model_dump_json() + "\n"
        return event.model_dump_json(exclude={"parent_event_id", "lamport"}) + "\n"

    def _next_display_seq(self, conn: sqlite3.Connection) -> int:
        """Return MAX(seq)+1 from the events table (git-mode live append)."""
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events").fetchone()
        return int(row[0])

    def _table_max_id(self, conn: sqlite3.Connection) -> int:
        """Return the numeric part of the MAX event id in the SQLite events table."""
        row = conn.execute(
            "SELECT MAX(CAST(SUBSTR(id, 2) AS INTEGER)) FROM events"
        ).fetchone()
        return row[0] if row and row[0] is not None else 0

    def _forward_catch_up(
        self,
        conn: sqlite3.Connection,
        *,
        from_seq: int,
        to_seq: int,
    ) -> None:
        """Re-apply log lines with ids in [from_seq, to_seq] to SQLite.

        Used during ``initialize()`` when the log is ahead of the projection
        (log-ahead skew from a crash after log-append but before COMMIT).
        Applies via ``_write_*`` only (same code path as replay), so there is
        no third apply implementation.

        Raises ``TransactionAborted`` (integrity alarm) if any target id is
        not found after scanning the entire log — the log is missing an event
        the projection expected to converge on.

        Audit side-effects in ``_write_*`` are suppressed during catch-up via
        the ``_replaying`` flag set by the caller (``initialize()`` sets it
        via ``replay_from_empty``, or ``initialize()`` sets it directly when
        catch-up runs outside of replay).
        """
        if not os.path.exists(self._events_path):
            if from_seq <= to_seq:
                raise TransactionAborted(
                    f"forward_catch_up: events.jsonl does not exist but "
                    f"expected events {from_seq}–{to_seq}."
                )
            return

        target_ids = {f"E{n:06d}" for n in range(from_seq, to_seq + 1)}

        with open(self._events_path, encoding="utf-8") as fh:
            for raw_line in fh:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    raw: dict[str, Any] = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                event_id = raw.get("id", "")
                if event_id not in target_ids:
                    continue
                try:
                    event = Event.model_validate(raw)
                except Exception as exc:
                    raise TransactionAborted(
                        f"forward_catch_up: cannot parse event {event_id!r}: {exc}"
                    ) from exc
                self._apply_write_only(conn, event)
                target_ids.discard(event_id)
                if not target_ids:
                    break

        if target_ids:
            raise TransactionAborted(
                f"forward_catch_up: log is missing events the projection expected "
                f"to converge on: {sorted(target_ids)}"
            )

    def _apply_write_only(
        self,
        conn: sqlite3.Connection,
        event: Event,
        *,
        seq: int | None = None,
    ) -> None:
        """Apply a single event via ``_write_*`` only — no validation, no logging.

        Used by ``replay_from_empty``, ``_replay_from_empty_git``, and
        ``_forward_catch_up``. Raises ``TransactionAborted`` on any failure so
        the caller knows the projection is inconsistent. ``seq`` is the
        replay-assigned display order (git mode); the local-mode callers leave
        it None.
        """
        action = event.action
        dispatch = self._get_action_dispatch()
        if action not in dispatch:
            raise TransactionAborted(
                f"_apply_write_only: unsupported action {action!r} during replay/catch-up."
            )
        spec = dispatch[action]
        try:
            typed_payload = spec.payload_model.model_validate(event.payload_json)
        except Exception as exc:
            raise TransactionAborted(
                f"_apply_write_only: payload parse failed for {action!r}: {exc}"
            ) from exc

        try:
            conn.execute("BEGIN IMMEDIATE")
            spec.write(conn, typed_payload, event)
            self._insert_event_row(conn, event, seq=seq)
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            self._safe_rollback(conn)
            if "database is locked" in str(exc).lower():
                raise StateLocked(
                    f"SQLite busy_timeout exceeded during replay of event {event.id!r}: {exc}"
                ) from exc
            raise TransactionAborted(
                f"Transaction aborted during replay of event {event.id!r}: {exc}"
            ) from exc
        except TransactionAborted:
            self._safe_rollback(conn)
            raise
        except Exception as exc:
            self._safe_rollback(conn)
            raise TransactionAborted(
                f"Transaction aborted during replay of event {event.id!r}: {exc}"
            ) from exc

    def _append_audit_line(
        self,
        kind: str,
        draft: EventDraft,
        reason: str,
        *,
        event_id: str | None = None,
    ) -> None:
        """Append a line to audit.jsonl (sibling of events.jsonl, never replayed).

        Shapes (spec section 4):
          rejection:             {ts, kind, actor, attempted_action, target_id, reason}
          idempotent_no_op:      {ts, kind, action, target_id, reason}
          write_failed_after_log:{ts, kind, event_id, action, target_id, reason}

        No ``id`` field — these are not events and never collide with the
        ``E######`` space.
        """
        audit_path = self._audit_path()
        now = self._clock.now().isoformat()
        if kind == "rejection":
            record: dict[str, Any] = {
                "ts": now,
                "kind": "rejection",
                "actor": draft.actor,
                "attempted_action": draft.action,
                "target_id": draft.target_id,
                "reason": reason,
            }
        elif kind == "idempotent_no_op":
            record = {
                "ts": now,
                "kind": "idempotent_no_op",
                "action": draft.action,
                "target_id": draft.target_id,
                "reason": reason,
            }
        elif kind == "write_failed_after_log":
            record = {
                "ts": now,
                "kind": "write_failed_after_log",
                "event_id": event_id or "",
                "action": draft.action,
                "target_id": draft.target_id,
                "reason": reason,
            }
        else:
            record = {
                "ts": now,
                "kind": kind,
                "action": draft.action,
                "target_id": draft.target_id,
                "reason": reason,
            }

        try:
            with open(audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            logger.error(
                "Failed to write audit line (kind=%r, action=%r): %s",
                kind,
                draft.action,
                exc,
            )

    def _audit_path(self) -> str:
        """Return the path to audit.jsonl (sibling of events.jsonl)."""
        events_dir = os.path.dirname(self._events_path)
        return os.path.join(events_dir, "audit.jsonl")

    # ------------------------------------------------------------------
    # Internal helpers — event routing
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Dispatch table — maps action name to (PayloadModel, bound handler).
    # Built lazily on first access to allow self-referential bound methods.
    # ------------------------------------------------------------------

    def _get_action_dispatch(self) -> dict[str, ActionSpec]:
        """Return the dispatch table mapping action → ``ActionSpec``.

        SL1-RR-1 architecture move #1: each action resolves to an
        ``ActionSpec(payload_model, check, write)`` rather than a single
        interleaved handler. The check/write phases share the normalised
        signature ``(conn, payload: TypedPayload, event: Event) -> None``; the
        payload is validated against ``payload_model`` before either phase runs.

        The table is built once per instance and cached. Bound-method values
        capture ``self``, so the cache is invalidated naturally if the instance
        is replaced.
        """
        cached: dict[str, ActionSpec] | None = getattr(
            self, "_action_dispatch_cache", None
        )
        if cached is not None:
            return cached
        table: dict[str, ActionSpec] = {
            "project.created": ActionSpec(
                ProjectCreatedPayload,
                self._check_project_created,
                self._write_project_created,
            ),
            "state.initialized": ActionSpec(
                StateInitializedPayload,
                self._check_audit_only,
                self._write_audit_only,
            ),
            "prd.parsed": ActionSpec(
                PrdParsedPayload, self._check_prd_parsed, self._write_prd_parsed
            ),
            # T023 — amend-aware, non-destructive PRD revision. Supersedes the
            # diff's retired requirements in place (NEVER DELETE), inserts the
            # added ones at the new revision, and bumps the per-PRD revision
            # counter. Registered alongside prd.parsed (the destructive
            # first-parse path).
            "prd.revised": ActionSpec(
                PrdRevisedPayload, self._check_prd_revised, self._write_prd_revised
            ),
            "prd.reviewed": ActionSpec(
                PrdReviewedPayload, self._check_prd_reviewed, self._write_prd_reviewed
            ),
            "prd.approved": ActionSpec(
                PrdApprovedPayload, self._check_prd_approved, self._write_prd_approved
            ),
            # T018 — decision back-propagation. Audit-only: the PRD source is
            # edited on disk and refreshed by a later prd.parsed; this event is
            # the immutable record that a decision was answered and written back.
            "prd.decision_resolved": ActionSpec(
                PrdDecisionResolvedPayload,
                self._check_audit_only,
                self._write_audit_only,
            ),
            "feature.created": ActionSpec(
                FeatureCreatedPayload,
                self._check_feature_created,
                self._write_feature_created,
            ),
            "task.created": ActionSpec(
                TaskCreatedPayload, self._check_task_created, self._write_task_created
            ),
            "task.scored": ActionSpec(
                TaskScoredPayload, self._check_task_scored, self._write_task_scored
            ),
            "task.expanded": ActionSpec(
                TaskExpandedPayload,
                self._check_task_expanded,
                self._write_task_expanded,
            ),
            "task.status_changed": ActionSpec(
                TaskStatusChangedPayload,
                self._check_task_status_changed,
                self._write_task_status_changed,
            ),
            # v1.15.0 — orphan cleanup on re-parse.
            "task.deleted": ActionSpec(
                TaskDeletedPayload, self._check_task_deleted, self._write_task_deleted
            ),
            "feature.deleted": ActionSpec(
                FeatureDeletedPayload,
                self._check_feature_deleted,
                self._write_feature_deleted,
            ),
            # CL-4 — persist ConflictGroups computed during planning/inference
            # so the conflict_groups table round-trips them.
            "conflict_group.upserted": ActionSpec(
                ConflictGroupUpsertedPayload,
                self._check_conflict_group_upserted,
                self._write_conflict_group_upserted,
            ),
            "bundle.created": ActionSpec(
                BundleCreatedPayload,
                self._check_bundle_created,
                self._write_bundle_created,
            ),
            "bundle.status_changed": ActionSpec(
                BundleStatusChangedPayload,
                self._check_bundle_status_changed,
                self._write_bundle_status_changed,
            ),
            "bundle.agent_observed": ActionSpec(
                BundleAgentObservedPayload,
                self._check_bundle_agent_observed,
                self._write_bundle_agent_observed,
            ),
            "bundle.review_recorded": ActionSpec(
                BundleReviewRecordedPayload,
                self._check_bundle_review_recorded,
                self._write_bundle_review_recorded,
            ),
            "bundle.plan_acknowledged": ActionSpec(
                BundlePlanAcknowledgedPayload,
                self._check_bundle_plan_acknowledged,
                self._write_audit_only,
            ),
            "bundle.checkpoint_recorded": ActionSpec(
                BundleCheckpointRecordedPayload,
                self._check_bundle_checkpoint_recorded,
                self._write_bundle_checkpoint_recorded,
            ),
            "bundle.superseded": ActionSpec(
                BundleSupersededPayload,
                self._check_bundle_superseded,
                self._write_bundle_superseded,
            ),
            "bundle.claimed": ActionSpec(
                BundleClaimedPayload,
                self._check_bundle_claimed,
                self._write_bundle_claimed,
            ),
            "bundle.progress_noted": ActionSpec(
                BundleProgressNotedPayload,
                self._check_bundle_progress_noted,
                self._write_audit_only,
            ),
            "bundle.claim_renewed": ActionSpec(
                BundleClaimRenewedPayload,
                self._check_bundle_claim_renewed,
                self._write_bundle_claim_renewed,
            ),
            "bundle.claim_released": ActionSpec(
                BundleClaimReleasedPayload,
                self._check_bundle_claim_released,
                self._write_bundle_claim_released,
            ),
            "bundle.claim_stale": ActionSpec(
                BundleClaimStalePayload,
                self._check_bundle_claim_stale,
                self._write_bundle_claim_stale,
            ),
            # Phase 8: pull-applies-remote — local Task gets title/desc/status
            # rewritten from the remote payload after a non-conflict pull.
            "task.synced_from_remote": ActionSpec(
                TaskSyncedFromRemotePayload,
                self._check_task_synced_from_remote,
                self._write_task_synced_from_remote,
            ),
            "claim.created": ActionSpec(
                ClaimCreatedPayload,
                self._check_claim_created,
                self._write_claim_created,
            ),
            "claim.released": ActionSpec(
                ClaimReleasedPayload,
                self._check_claim_released,
                self._write_claim_released,
            ),
            "claim.renewed": ActionSpec(
                ClaimRenewedPayload,
                self._check_claim_renewed,
                self._write_claim_renewed,
            ),
            "claim.stale": ActionSpec(
                ClaimStalePayload, self._check_claim_stale, self._write_claim_stale
            ),
            "evidence.submitted": ActionSpec(
                EvidenceSubmittedPayload,
                self._check_evidence_submitted,
                self._write_evidence_submitted,
            ),
            "task.applied": ActionSpec(
                TaskAppliedPayload, self._check_task_applied, self._write_task_applied
            ),
            "file_changed": ActionSpec(
                FileChangedPayload, self._check_audit_only, self._write_audit_only
            ),
            # Phase 6: MCP submit_progress — audit-only, no SQLite mutation.
            "progress.noted": ActionSpec(
                ProgressNotedPayload, self._check_audit_only, self._write_audit_only
            ),
            # Phase 8: sync_mappings table — external-system mirroring.
            "sync_mapping.upserted": ActionSpec(
                SyncMappingUpsertedPayload,
                self._check_sync_mapping_upserted,
                self._write_sync_mapping_upserted,
            ),
            "sync_mapping.deleted": ActionSpec(
                SyncMappingDeletedPayload,
                self._check_sync_mapping_deleted,
                self._write_sync_mapping_deleted,
            ),
            # Phase 8 Wave 3: sync.* audit events (CLI sync surface). Every
            # one is an audit-only no-op; the JSONL row is the entire audit
            # record. State mutation flows through the `sync_mapping.upserted`
            # event above, kept separate so replay can rebuild the mappings
            # table without `sync.*` semantics.
            #
            # Phase 9 T3/T5: ``SyncAuditPayload`` is now a discriminated
            # union (TypeAlias), NOT a BaseModel subclass — calling
            # ``SyncAuditPayload.model_validate(...)`` from the dispatcher
            # raises ``AttributeError`` because ``types.UnionType`` has no
            # such classmethod. We dispatch each ``sync.*`` action against its
            # concrete subclass via ``ACTION_TO_PAYLOAD`` from ``payloads.py``
            # so every entry resolves to a real ``BaseModel`` class with a
            # working ``.model_validate``. This also tightens validation: each
            # subclass declares only the fields its action actually carries
            # (``extra='forbid'``), so malformed payloads fail fast at dispatch.
            **{
                action: ActionSpec(
                    model_cls, self._check_audit_only, self._write_audit_only
                )
                for action, model_cls in ACTION_TO_PAYLOAD.items()
            },
        }
        self._action_dispatch_cache = table
        return table

    # ------------------------------------------------------------------
    # Audit-only phases — shared by state.initialized, file_changed,
    # progress.noted, and every sync.* action. The JSONL row is the entire
    # audit record; there is no SQLite mutation. The check always proceeds and
    # the write is a no-op.
    # ------------------------------------------------------------------

    def _check_audit_only(
        self,
        conn: sqlite3.Connection,
        payload: BaseModel,
        event: EventDraft,
    ) -> None:
        """No-op check for audit-only actions — always proceeds.

        Payload validation (the model + ``extra='forbid'``) already ran in
        An audit-only action has no state precondition that
        could reject it, so this phase never raises.
        """
        _ = (conn, payload, event)

    def _write_audit_only(
        self,
        conn: sqlite3.Connection,
        payload: BaseModel,
        event: Event,
    ) -> None:
        """No-op write for audit-only actions — the event row is the record.

        Covers ``state.initialized``, ``file_changed``, ``progress.noted`` and
        every ``sync.*`` action. The events-table INSERT + JSONL line (written
        by the caller) are the entire audit trail; no domain table is touched.
        """
        _ = (conn, payload, event)

    def _check_project_created(
        self,
        conn: sqlite3.Connection,
        payload: ProjectCreatedPayload,
        event: EventDraft,
    ) -> None:
        """No validation gate — project.created is an idempotent upsert."""
        _ = (conn, payload, event)

    def _write_project_created(
        self,
        conn: sqlite3.Connection,
        payload: ProjectCreatedPayload,
        event: Event,
    ) -> None:
        """Insert or replace the project row from the event payload."""
        _ = event
        project = Project.model_validate(payload.model_dump(mode="json"))
        data = project.model_dump(mode="json")
        conn.execute(
            """
            INSERT OR REPLACE INTO projects
                (id, name, description, created_at, updated_at)
            VALUES
                (:id, :name, :description, :created_at, :updated_at)
            """,
            data,
        )

    # ------------------------------------------------------------------
    # Phase 3 handlers
    # ------------------------------------------------------------------

    def _check_prd_parsed(
        self,
        conn: sqlite3.Connection,
        payload: PrdParsedPayload,
        event: EventDraft,
    ) -> None:
        """Validate every Requirement payload before any write.

        Was a validation guard inside the old handler (``raise
        TransactionAborted`` on an invalid Requirement); now rejects up front.
        """
        _ = (conn, event)
        requirements = self._validate_requirement_payloads(
            payload.requirements,
            action="prd.parsed",
        )
        self._validate_prd_assumptions(
            payload.assumptions,
            {requirement.id for requirement in requirements},
            action="prd.parsed",
        )

    @staticmethod
    def _validate_requirement_payloads(
        raw_requirements: Iterable[Any],
        *,
        action: str,
    ) -> list[Requirement]:
        """Validate requirement payloads once and retain canonical models."""
        requirements: list[Requirement] = []
        for req_data in raw_requirements:
            try:
                requirements.append(Requirement.model_validate(req_data))
            except Exception as exc:
                raise EventRejected(
                    f"{action}: invalid Requirement in payload: {exc}"
                ) from exc
        return requirements

    @staticmethod
    def _validate_prd_assumptions(
        assumptions: list[PRDAssumption],
        requirement_ids: set[str],
        *,
        action: str,
    ) -> None:
        """Validate typed assumptions without changing legacy event semantics."""
        seen: set[str] = set()
        for assumption in assumptions:
            if assumption.id in seen:
                raise EventRejected(
                    f"{action}: duplicate assumption id {assumption.id!r}"
                )
            seen.add(assumption.id)
            unknown = sorted(set(assumption.requirement_ids) - requirement_ids)
            if unknown:
                raise EventRejected(
                    f"{action}: assumption {assumption.id!r} references unknown "
                    f"requirement(s): {', '.join(unknown)}"
                )

    def _write_prd_parsed(
        self,
        conn: sqlite3.Connection,
        payload: PrdParsedPayload,
        event: Event,
    ) -> None:
        """Upsert PRD and destructively replace all requirements.

        Payload fields (all required):
            project_id (str)  — FK into projects table
            status (str)      — PRDStatus value (default 'draft' if absent)
            summary (str)
            goals (list[str])
            non_goals (list[str])
            requirements (list[dict]) — each is a Requirement payload
            acceptance_criteria (list[str])
            risks (list[str])
            open_questions (list[str])

        The ``requirements`` list in the PRD payload contains full Requirement
        dicts.  The top-level ``prds.requirements`` column stores only the list
        of requirement IDs (FK-style); the actual Requirement rows live in the
        ``requirements`` table.

        Parsing is destructive: old Requirement rows are deleted and replaced
        with the new set inside a SAVEPOINT so failure leaves no partial state.

        Each Requirement was already validated by ``_check_prd_parsed``; the
        ``model_validate`` calls here are an infallible rebuild.
        """
        _ = event
        project_id: str = payload.project_id
        summary: str = payload.summary
        status: str = payload.status
        goals = payload.goals
        non_goals = payload.non_goals
        requirements_raw: list[Any] = payload.requirements
        acceptance_criteria = payload.acceptance_criteria
        risks = payload.risks
        open_questions = payload.open_questions
        assumptions = [
            assumption.model_dump(mode="json")
            for assumption in payload.assumptions
        ]

        # The payload-level prd_id is the authoritative partition this parse
        # writes into (DEFAULT_PRD_ID='default' for a pre-v7 replay or a
        # single-PRD project). Requirement rows are stamped with it so a
        # per-PRD re-parse clears and rewrites only its own partition.
        prd_id: str = payload.prd_id

        requirement_objects: list[Requirement] = [
            Requirement.model_validate(req_data) for req_data in requirements_raw
        ]
        requirement_ids = [r.id for r in requirement_objects]

        # Upsert the PRD row keyed on ``id`` = ``prd_id`` (BUG 001). Under the v7
        # single-column PK (``id TEXT PRIMARY KEY DEFAULT 'default'``) a bare
        # INSERT OR REPLACE that omitted id/is_default/created_at/updated_at
        # would collide on the migrated ``id='default'`` row and, being a
        # DELETE+INSERT, reset is_default 1→0 and wipe created_at/updated_at —
        # destroying the v7 migration's COALESCE backfill and the
        # ux_prds_default invariant on the first re-parse. Use an explicit UPSERT
        # that writes ONLY the v6 content columns on conflict, preserving
        # is_default / created_at / the identity columns of the existing row.
        #
        # For a pre-v7 replay the payload defaults (prd_id='default',
        # is_default=True, title='', target_version/target_tag=None) reproduce
        # the prior literal INSERT byte-for-byte, so the golden is unchanged.
        #
        # ``now`` comes from the event timestamp (the backend's injected clock
        # source — see the many ``event.timestamp.isoformat()`` write handlers)
        # so replay is deterministic; we never call datetime.now() directly.
        now = event.timestamp.isoformat()
        conn.execute(
            """
            INSERT INTO prds
                (id, project_id, title, status, summary, goals, non_goals,
                 requirements, acceptance_criteria, risks, open_questions,
                 assumptions,
                 last_reviewed_at, last_reviewed_by,
                 target_version, target_tag,
                 is_default, created_at, updated_at)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                project_id = excluded.project_id,
                title = excluded.title,
                status = excluded.status,
                summary = excluded.summary,
                goals = excluded.goals,
                non_goals = excluded.non_goals,
                requirements = excluded.requirements,
                acceptance_criteria = excluded.acceptance_criteria,
                risks = excluded.risks,
                open_questions = excluded.open_questions,
                assumptions = excluded.assumptions,
                last_reviewed_at = excluded.last_reviewed_at,
                last_reviewed_by = excluded.last_reviewed_by,
                target_version = excluded.target_version,
                target_tag = excluded.target_tag,
                updated_at = excluded.updated_at
            """,
            (
                prd_id,
                project_id,
                payload.title,
                status,
                summary,
                json.dumps(goals),
                json.dumps(non_goals),
                json.dumps(requirement_ids),
                json.dumps(acceptance_criteria),
                json.dumps(risks),
                json.dumps(open_questions),
                json.dumps(assumptions),
                payload.target_version,
                payload.target_tag,
                1 if payload.is_default else 0,
                now,
                now,
            ),
        )

        # Destructive re-parse of requirements, SCOPED to this PRD's partition —
        # use SAVEPOINT so failure is atomic within the outer transaction. The
        # scoped DELETE (WHERE prd_id = ?) clears only this PRD's requirements so
        # a per-PRD re-parse never wipes another partition; the default PRD
        # clears exactly 'default'. (Pre-v7 replay: prd_id='default', so the
        # WHERE matches every row the prior unscoped DELETE removed.)
        conn.execute("SAVEPOINT prd_requirements_replace")
        try:
            conn.execute(
                "DELETE FROM requirements WHERE prd_id = ?", (prd_id,)
            )
            for req in requirement_objects:
                conn.execute(
                    """
                    INSERT INTO requirements
                        (id, prd_id, prd_section, text, source_paragraph, derived)
                    VALUES
                        (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        req.id,
                        prd_id,
                        req.prd_section,
                        req.text,
                        req.source_paragraph,
                        1 if req.derived else 0,
                    ),
                )
        except Exception:
            conn.execute("ROLLBACK TO prd_requirements_replace")
            conn.execute("RELEASE prd_requirements_replace")
            raise
        conn.execute("RELEASE prd_requirements_replace")

    def _check_prd_revised(
        self,
        conn: sqlite3.Connection,
        payload: PrdRevisedPayload,
        event: EventDraft,
    ) -> None:
        """Validate a prd.revised before any write.

        Gates, all up front (no partial state):

        1. ``revision`` must be exactly ``current + 1`` — the per-PRD revision
           counter is a strict monotonic +1 sequence, so a stale or skipped
           revision (e.g. two agents revising the same PRD off the same base)
           is rejected rather than silently mis-stamping lineage. The target
           PRD must exist; revising an unknown ``prd_id`` is rejected.
        2. Every requirement dict in the added / superseded / unchanged diff
           lists must be a valid Requirement (mirrors ``_check_prd_parsed``).
        3. The diff must match the on-disk live set so the write applies
           cleanly and the status-demotion rule keys off a REAL change:
           - every ``requirements_superseded`` / ``requirements_unchanged`` id
             must currently be live (``revision_superseded IS NULL``) in this
             PRD's partition — superseding/carrying an id that is missing or
             already retired is rejected (it would otherwise UPDATE 0 rows yet
             still demote status / claim to carry an edit forward);
           - every ``requirements_added`` id must NOT already exist in this
             partition (live OR superseded). The requirements PK is the single
             ``id`` column, so a re-added id would otherwise collide — destroying
             the existing lineage row (INSERT OR REPLACE) or raising mid-write
             (plain INSERT). Rejecting up front keeps lineage append-only.
        """
        _ = event
        row = conn.execute(
            "SELECT revision FROM prds WHERE id = ?", (payload.prd_id,)
        ).fetchone()
        if row is None:
            raise EventRejected(
                f"prd.revised: unknown prd_id {payload.prd_id!r} — cannot revise "
                "a PRD that does not exist"
            )
        current = int(row[0])
        if payload.revision != current + 1:
            raise EventRejected(
                f"prd.revised: revision {payload.revision} is not current+1 "
                f"(current revision is {current}, expected {current + 1})"
            )
        requirements_added = self._validate_requirement_payloads(
            payload.requirements_added,
            action="prd.revised",
        )
        requirements_superseded = self._validate_requirement_payloads(
            payload.requirements_superseded,
            action="prd.revised",
        )
        requirements_unchanged = self._validate_requirement_payloads(
            payload.requirements_unchanged,
            action="prd.revised",
        )
        revised_requirement_ids = {
            requirement.id
            for requirement in (*requirements_added, *requirements_unchanged)
        }
        self._validate_prd_assumptions(
            payload.assumptions,
            revised_requirement_ids,
            action="prd.revised",
        )

        # Snapshot this PRD's partition: which ids exist at all, and which are
        # currently live. The diff is validated against these so the write path
        # is a pure, total application (no 0-row UPDATEs, no PK collisions).
        all_ids = {
            r[0]
            for r in conn.execute(
                "SELECT id FROM requirements WHERE prd_id = ?", (payload.prd_id,)
            ).fetchall()
        }
        live_ids = {
            r[0]
            for r in conn.execute(
                "SELECT id FROM requirements "
                "WHERE prd_id = ? AND revision_superseded IS NULL",
                (payload.prd_id,),
            ).fetchall()
        }
        for requirement in requirements_superseded:
            rid = requirement.id
            if rid not in live_ids:
                raise EventRejected(
                    f"prd.revised: cannot supersede requirement {rid!r} — it is "
                    "not currently live in this PRD (missing or already "
                    "superseded)"
                )
        for requirement in requirements_unchanged:
            rid = requirement.id
            if rid not in live_ids:
                raise EventRejected(
                    f"prd.revised: requirements_unchanged names {rid!r}, which is "
                    "not currently live in this PRD — cannot carry it forward"
                )
        for requirement in requirements_added:
            rid = requirement.id
            if rid in all_ids:
                raise EventRejected(
                    f"prd.revised: cannot add requirement {rid!r} — that id "
                    "already exists in this PRD (re-adding a retired id would "
                    "destroy its lineage row); use a fresh id"
                )

    def _write_prd_revised(
        self,
        conn: sqlite3.Connection,
        payload: PrdRevisedPayload,
        event: Event,
    ) -> None:
        """Apply a non-destructive, amend-aware PRD revision.

        Unlike ``prd.parsed`` (which DELETEs and re-inserts every requirement),
        ``prd.revised`` NEVER deletes a requirement row — the requirements table
        is the append-only lineage of the PRD:

        - UPDATE the prds scalar columns from the payload and bump ``revision``
          to the new revision number.
        - For each requirement in ``requirements_superseded``: stamp
          ``revision_superseded = <new revision>`` on the existing row. The row
          stays in the table (it drops out of the live set — see
          ``list_requirements`` — but its lineage survives replay).
        - For each requirement in ``requirements_added``: INSERT a new row with
          ``revision_introduced = <new revision>`` and ``revision_superseded``
          NULL (live). ``_check_prd_revised`` has already rejected any added id
          that collides with an existing row, so a plain INSERT is safe and
          NEVER overwrites a lineage row (a re-added id would destroy the
          retired row's history — see the handler's append-only contract).
        - For each requirement in ``requirements_unchanged``: UPDATE the live
          carried-forward row's editable fields in place (text / prd_section /
          source_paragraph / derived). The id, prd_id and lineage stamps are
          untouched. This lets a revision edit a requirement it is carrying
          forward; without it the field is dead and the edit is silently lost.
        - Status-demotion rule: a revision that supersedes/removes any
          requirement or changes a recorded assumption demotes an approved PRD
          back to ``draft``. Assumption changes alter the reviewed product
          contract even when the requirement diff is otherwise empty.

        The whole body runs inside a SAVEPOINT so a failure mid-revision leaves
        no partial state within the outer transaction.
        """
        prd_id: str = payload.prd_id
        new_revision: int = payload.revision

        superseded_objects = [
            Requirement.model_validate(r) for r in payload.requirements_superseded
        ]
        added_objects = [
            Requirement.model_validate(r) for r in payload.requirements_added
        ]
        unchanged_objects = [
            Requirement.model_validate(r) for r in payload.requirements_unchanged
        ]

        current_assumptions_row = conn.execute(
            "SELECT assumptions FROM prds WHERE id = ?",
            (prd_id,),
        ).fetchone()
        current_assumptions = (
            json.loads(current_assumptions_row[0])
            if current_assumptions_row is not None
            else []
        )
        revised_assumptions = [
            assumption.model_dump(mode="json") for assumption in payload.assumptions
        ]

        # Superseding/removing a requirement or changing an assumption is a
        # material product-contract change, so an approved PRD drops to draft.
        status = payload.status
        if superseded_objects or revised_assumptions != current_assumptions:
            status = "draft"

        now = event.timestamp.isoformat()

        conn.execute("SAVEPOINT prd_revised")
        try:
            # UPDATE the scalar PRD columns and bump the revision counter. We do
            # NOT touch id / is_default / created_at — a revision targets an
            # EXISTING row and must preserve its identity and the
            # ux_prds_default invariant (see PrdRevisedPayload.is_default).
            conn.execute(
                """
                UPDATE prds
                   SET project_id = ?,
                       title = ?,
                       status = ?,
                       summary = ?,
                       goals = ?,
                       non_goals = ?,
                       acceptance_criteria = ?,
                       risks = ?,
                       open_questions = ?,
                       assumptions = ?,
                       target_version = ?,
                       target_tag = ?,
                       revision = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    payload.project_id,
                    payload.title,
                    status,
                    payload.summary,
                    json.dumps(payload.goals),
                    json.dumps(payload.non_goals),
                    json.dumps(payload.acceptance_criteria),
                    json.dumps(payload.risks),
                    json.dumps(payload.open_questions),
                    json.dumps(revised_assumptions),
                    payload.target_version,
                    payload.target_tag,
                    new_revision,
                    now,
                    prd_id,
                ),
            )

            # Supersede retired requirements in place — NEVER DELETE. Stamp the
            # revision that retired them; scoped to this PRD's partition and to
            # rows still live so a re-stamp is idempotent.
            for req in superseded_objects:
                conn.execute(
                    """
                    UPDATE requirements
                       SET revision_superseded = ?
                     WHERE id = ? AND prd_id = ?
                       AND revision_superseded IS NULL
                    """,
                    (new_revision, req.id, prd_id),
                )

            # Apply any edits carried in requirements_unchanged onto the live
            # rows in place. _check_prd_revised guarantees each id is currently
            # live, so this UPDATE always targets exactly one row; the lineage
            # stamps and id/prd_id are untouched. Without this the field is dead
            # and an edit on a carried-forward requirement is silently dropped.
            for req in unchanged_objects:
                conn.execute(
                    """
                    UPDATE requirements
                       SET prd_section = ?,
                           text = ?,
                           source_paragraph = ?,
                           derived = ?
                     WHERE id = ? AND prd_id = ?
                       AND revision_superseded IS NULL
                    """,
                    (
                        req.prd_section,
                        req.text,
                        req.source_paragraph,
                        1 if req.derived else 0,
                        req.id,
                        prd_id,
                    ),
                )

            # Insert the requirements new in this revision, stamped with the
            # revision they were introduced at and live (revision_superseded
            # NULL). Plain INSERT — _check_prd_revised has rejected any added id
            # that collides with an existing (live or superseded) row, so this
            # never overwrites a lineage row.
            for req in added_objects:
                conn.execute(
                    """
                    INSERT INTO requirements
                        (id, prd_id, prd_section, text, source_paragraph,
                         derived, revision_introduced, revision_superseded)
                    VALUES
                        (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        req.id,
                        prd_id,
                        req.prd_section,
                        req.text,
                        req.source_paragraph,
                        1 if req.derived else 0,
                        new_revision,
                    ),
                )

            # Refresh the prds.requirements FK list to the live set in stable id
            # order so it mirrors the requirements actually current after this
            # revision (added + carried-forward, minus superseded).
            live_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM requirements "
                    "WHERE prd_id = ? AND revision_superseded IS NULL "
                    "ORDER BY id ASC",
                    (prd_id,),
                ).fetchall()
            ]
            conn.execute(
                "UPDATE prds SET requirements = ? WHERE id = ?",
                (json.dumps(live_ids), prd_id),
            )
        except Exception:
            conn.execute("ROLLBACK TO prd_revised")
            conn.execute("RELEASE prd_revised")
            raise
        conn.execute("RELEASE prd_revised")

    def _check_prd_reviewed(
        self,
        conn: sqlite3.Connection,
        payload: PrdReviewedPayload,
        event: EventDraft,
    ) -> None:
        """No state precondition — the UPDATE is scoped and side-effect-only."""
        _ = (conn, payload, event)

    def _write_prd_reviewed(
        self,
        conn: sqlite3.Connection,
        payload: PrdReviewedPayload,
        event: Event,
    ) -> None:
        """Mark PRD as reviewed.

        Payload fields:
            project_id (str) — required (scopes the UPDATE so multi-PRD
                              setups in future phases don't co-mutate)
            reviewer (str)   — required
            notes (str | None) — optional

        We deliberately do NOT insert into the reviews table here. The
        prds.status column transitioning draft → reviewed is its own audit
        record. The reviews table is reserved for outcome-bearing review
        decisions (approve, reject, needs_changes). Recording prd.reviewed
        as decision='approve' would make it indistinguishable from a real
        approval and cause false positives for any downstream code (e.g.,
        the Phase 4 claims manager) that queries
        `reviews WHERE decision='approve'` to determine approval state.
        """
        project_id: str = payload.project_id
        prd_id: str = payload.prd_id
        reviewer: str = payload.reviewer
        timestamp: str = event.timestamp.isoformat()

        # Scope to project_id AND id so a multi-PRD project mutates only the
        # named PRD. Pre-v7 replay: prd_id='default' matches the single migrated
        # row, so the result is byte-identical to the old project_id-only UPDATE.
        conn.execute(
            """
            UPDATE prds
               SET status = 'reviewed',
                   last_reviewed_at = ?,
                   last_reviewed_by = ?,
                   updated_at = ?
             WHERE project_id = ? AND id = ?
            """,
            (timestamp, reviewer, timestamp, project_id, prd_id),
        )

    def _check_prd_approved(
        self,
        conn: sqlite3.Connection,
        payload: PrdApprovedPayload,
        event: EventDraft,
    ) -> None:
        """No state precondition — scoped UPDATE plus an idempotent Review upsert."""
        _ = (conn, payload, event)

    def _write_prd_approved(
        self,
        conn: sqlite3.Connection,
        payload: PrdApprovedPayload,
        event: Event,
    ) -> None:
        """Mark PRD as approved and insert an approval Review row.

        Payload fields:
            project_id (str) — required (scopes the UPDATE)
            approver (str)   — required

        The Review row ID is derived deterministically from the event_id so
        that replay produces byte-for-byte identical rows. This is the
        canonical 'approved' marker — queries should use the PRD's status
        column OR look for reviews WHERE target_id=<project_id> AND
        decision='approve' AND target_kind='prd'.
        """
        project_id: str = payload.project_id
        prd_id: str = payload.prd_id
        approver: str = payload.approver
        event_id: str = event.id
        timestamp: str = event.timestamp.isoformat()

        # Scope to project_id AND id (see _write_prd_reviewed). Pre-v7 replay:
        # prd_id='default' matches the single migrated row → byte-identical.
        conn.execute(
            """
            UPDATE prds
               SET status = 'approved',
                   last_reviewed_at = ?,
                   last_reviewed_by = ?,
                   updated_at = ?
             WHERE project_id = ? AND id = ?
            """,
            (timestamp, approver, timestamp, project_id, prd_id),
        )

        review_id = f"RV-{event_id}"
        conn.execute(
            """
            INSERT OR REPLACE INTO reviews
                (id, target_kind, target_id, reviewed_by, decision, notes, created_at)
            VALUES
                (?, 'prd', ?, ?, 'approve', NULL, ?)
            """,
            (review_id, project_id, approver, timestamp),
        )

    def _check_feature_created(
        self,
        conn: sqlite3.Connection,
        payload: FeatureCreatedPayload,
        event: EventDraft,
    ) -> None:
        """Validate the Feature payload before any write.

        Was a validation guard inside the old handler (``raise
        TransactionAborted`` on an invalid Feature); now rejects up front.
        """
        _ = (conn, event)
        try:
            Feature.model_validate(payload.model_dump(mode="json"))
        except Exception as exc:
            raise EventRejected(
                f"feature.created: invalid Feature payload: {exc}"
            ) from exc

    def _write_feature_created(
        self,
        conn: sqlite3.Connection,
        payload: FeatureCreatedPayload,
        event: Event,
    ) -> None:
        """Insert a Feature row from the event payload.

        Payload fields: all Feature model fields (id, title, description,
        status, requirements, tasks).

        The payload was already validated by ``_check_feature_created``; the
        ``model_validate`` here is an infallible rebuild.
        """
        _ = event
        feature = Feature.model_validate(payload.model_dump(mode="json"))
        data = feature.model_dump(mode="json")
        # prd_id is Field(exclude=True) on the model, so it is NOT in
        # model_dump(); read it as the in-memory attribute and write the column
        # explicitly. Pre-v7 replay carries the DEFAULT_PRD_ID='default' default.
        prd_id = feature.prd_id
        # Use INSERT ... ON CONFLICT DO UPDATE (UPSERT) instead of INSERT OR
        # REPLACE to avoid violating the ON DELETE RESTRICT FK from tasks.
        # INSERT OR REPLACE is equivalent to DELETE + INSERT which trips the FK
        # when tasks already reference this feature.
        conn.execute(
            """
            INSERT INTO features
                (id, prd_id, title, description, status, requirements, tasks)
            VALUES
                (:id, :prd_id, :title, :description, :status, :requirements, :tasks)
            ON CONFLICT(id) DO UPDATE SET
                prd_id       = excluded.prd_id,
                title        = excluded.title,
                description  = excluded.description,
                status       = excluded.status,
                requirements = excluded.requirements,
                tasks        = excluded.tasks
            """,
            {
                "id": data["id"],
                "prd_id": prd_id,
                "title": data["title"],
                "description": data["description"],
                "status": data["status"],
                "requirements": json.dumps(data["requirements"]),
                "tasks": json.dumps(data["tasks"]),
            },
        )

    def _check_conflict_group_upserted(
        self,
        conn: sqlite3.Connection,
        payload: ConflictGroupUpsertedPayload,
        event: EventDraft,
    ) -> None:
        """Validate the ConflictGroup payload before any write (CL-4)."""
        _ = (conn, event)
        try:
            ConflictGroup.model_validate(payload.model_dump(mode="json"))
        except Exception as exc:
            raise EventRejected(
                f"conflict_group.upserted: invalid ConflictGroup payload: {exc}"
            ) from exc

    def _write_conflict_group_upserted(
        self,
        conn: sqlite3.Connection,
        payload: ConflictGroupUpsertedPayload,
        event: Event,
    ) -> None:
        """Insert/replace a ConflictGroup row from the event payload (CL-4).

        Payload fields map directly to the ConflictGroup model (id, name,
        task_ids, reason). Idempotent UPSERT keyed on ``id`` so re-planning the
        same task graph rewrites the group in place rather than erroring.
        """
        _ = event
        group = ConflictGroup.model_validate(payload.model_dump(mode="json"))
        data = group.model_dump(mode="json")
        conn.execute(
            """
            INSERT INTO conflict_groups (id, name, task_ids, reason)
            VALUES (:id, :name, :task_ids, :reason)
            ON CONFLICT(id) DO UPDATE SET
                name     = excluded.name,
                task_ids = excluded.task_ids,
                reason   = excluded.reason
            """,
            {
                "id": data["id"],
                "name": data["name"],
                "task_ids": json.dumps(data["task_ids"]),
                "reason": data["reason"],
            },
        )

    @staticmethod
    def _event_schema_version(
        conn: sqlite3.Connection, event_id: str | None
    ) -> int | None:
        if event_id is None:
            return None
        row = conn.execute(
            "SELECT payload_json FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0]).get("schema_version")
            return int(value) if value is not None else None
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _bundle_generation_is_legacy(
        conn: sqlite3.Connection, creation_event_id: str
    ) -> bool:
        version = SqliteBackend._event_schema_version(conn, creation_event_id)
        return version is None or version < 13

    @staticmethod
    def _bundle_disposition_is_legacy(
        conn: sqlite3.Connection, bundle_id: str, creation_event_id: str
    ) -> bool:
        row = conn.execute(
            "SELECT review_disposition_event_id FROM execution_bundles "
            "WHERE id = ? AND creation_event_id = ?",
            (bundle_id, creation_event_id),
        ).fetchone()
        if row is None or row[0] is None:
            return False
        return SqliteBackend._event_schema_version(conn, row[0]) is None

    def _check_bundle_created(
        self,
        conn: sqlite3.Connection,
        payload: BundleCreatedPayload,
        event: EventDraft,
    ) -> None:
        """Validate complete bundle membership against a fresh WAL snapshot."""
        if event.target_kind != "bundle" or event.target_id != payload.id:
            raise EventRejected(
                "bundle.created: event target must be bundle "
                f"'{payload.id}', got {event.target_kind} '{event.target_id}'."
            )
        if payload.schema_version != SCHEMA_VERSION:
            raise EventRejected(
                f"bundle.created: schema_version must be {SCHEMA_VERSION}."
            )
        event_time = event.timestamp.astimezone(datetime.UTC)
        if payload.created_at != event_time or payload.updated_at != event_time:
            raise EventRejected(
                "bundle.created: creation and update times must match the event time."
            )
        try:
            bundle_data = payload.model_dump(mode="json")
            bundle_data.pop("schema_version", None)
            # The log authority assigns the real creation_event_id only after
            # this pre-log check succeeds. A placeholder completes the state
            # model for relational validation without trusting caller input.
            bundle_data["creation_event_id"] = "E000000"
            bundle = ExecutionBundle.model_validate(bundle_data)
        except Exception as exc:
            raise EventRejected(f"bundle.created: invalid bundle payload: {exc}") from exc
        if bundle.status is not BundleStatus.planned:
            raise EventRejected("bundle.created: initial status must be 'planned'.")

        # Mirror claim.created's fresh-snapshot discipline. A competing claim or
        # bundle append may have committed after an earlier read on this WAL
        # connection; BEGIN IMMEDIATE refreshes the snapshot under the append
        # flock before any canonical log line is written.
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_bundle_created_locked(conn, bundle)
        finally:
            conn.execute("COMMIT")

    @staticmethod
    def _validate_bundle_created_locked(
        conn: sqlite3.Connection, bundle: ExecutionBundle
    ) -> None:
        if conn.execute(
            "SELECT 1 FROM execution_bundles WHERE id = ?", (bundle.id,)
        ).fetchone():
            raise EventRejected(f"bundle.created: bundle '{bundle.id}' already exists.")

        if conn.execute(
            "SELECT 1 FROM prds WHERE id = ?", (bundle.prd_id,)
        ).fetchone() is None:
            raise EventRejected(
                f"bundle.created: owning PRD '{bundle.prd_id}' not found."
            )

        placeholders = ",".join("?" for _ in bundle.task_ids)
        task_rows = conn.execute(
            f"SELECT id, prd_id FROM tasks WHERE id IN ({placeholders})",
            tuple(bundle.task_ids),
        ).fetchall()
        tasks_by_id = {row[0]: row[1] for row in task_rows}
        missing = [task_id for task_id in bundle.task_ids if task_id not in tasks_by_id]
        if missing:
            raise EventRejected(f"bundle.created: member tasks not found: {missing}.")
        cross_prd = [
            task_id
            for task_id in bundle.task_ids
            if tasks_by_id[task_id] != bundle.prd_id
        ]
        if cross_prd:
            raise EventRejected(
                f"bundle.created: member tasks belong to another PRD: {cross_prd}."
            )

        terminal = tuple(status.value for status in TERMINAL_BUNDLE_STATUSES)
        terminal_placeholders = ",".join("?" for _ in terminal)
        membership_rows = conn.execute(
            f"""SELECT m.task_id, b.id
                  FROM execution_bundle_members m
                  JOIN execution_bundles b ON b.id = m.bundle_id
                 WHERE m.task_id IN ({placeholders})
                   AND b.status NOT IN ({terminal_placeholders})
                   AND b.status != 'replan_required'
                 ORDER BY m.task_id, b.id""",
            tuple(bundle.task_ids) + terminal,
        ).fetchall()
        if membership_rows:
            conflicts = [(row[0], row[1]) for row in membership_rows]
            raise EventRejected(
                f"bundle.created: member tasks already belong to active bundles: {conflicts}."
            )

        claim_rows = conn.execute(
            f"SELECT task_id, id, claimed_by FROM claims "
            f"WHERE status = 'active' AND task_id IN ({placeholders}) "
            "ORDER BY task_id, id",
            tuple(bundle.task_ids),
        ).fetchall()
        if claim_rows:
            claims = [(row[0], row[1], row[2]) for row in claim_rows]
            raise EventRejected(
                f"bundle.created: member tasks have incompatible active claims: {claims}."
            )

    @staticmethod
    def _write_bundle_created(
        conn: sqlite3.Connection,
        payload: BundleCreatedPayload,
        event: Event,
    ) -> None:
        """Persist the complete creation fact and ordered membership."""
        if event.target_kind != "bundle" or event.target_id != payload.id:
            return
        event_time = event.timestamp.astimezone(datetime.UTC)
        if payload.created_at != event_time or payload.updated_at != event_time:
            return
        bundle_data = payload.model_dump(mode="json")
        bundle_data.pop("schema_version", None)
        bundle_data["creation_event_id"] = event.id
        bundle = ExecutionBundle.model_validate(bundle_data)
        # Git-backed histories may contain two independently valid creations
        # for the same stable id. Replay is write-only, so converge by keeping
        # the first HLC-ordered creation as one atomic fact. Never combine the
        # first row with later membership, which would synthesize a bundle that
        # appeared in neither branch.
        if conn.execute(
            "SELECT 1 FROM execution_bundles WHERE id = ?", (bundle.id,)
        ).fetchone():
            return
        placeholders = ",".join("?" for _ in bundle.task_ids)
        terminal = tuple(status.value for status in TERMINAL_BUNDLE_STATUSES)
        terminal_placeholders = ",".join("?" for _ in terminal)
        existing_membership = conn.execute(
            f"""SELECT 1
                  FROM execution_bundle_members m
                  JOIN execution_bundles b ON b.id = m.bundle_id
                 WHERE m.task_id IN ({placeholders})
                   AND b.status NOT IN ({terminal_placeholders})
                   AND b.status != 'replan_required'
                 LIMIT 1""",
            tuple(bundle.task_ids) + terminal,
        ).fetchone()
        if existing_membership is not None:
            # Two git branches can independently create different bundle IDs
            # around the same task. The ordered replay winner owns the complete
            # membership fact; skip the loser atomically rather than projecting
            # two active bundles that live append would have refused.
            return
        if conn.execute(
            f"SELECT 1 FROM claims WHERE status = 'active' "
            f"AND task_id IN ({placeholders}) LIMIT 1",
            tuple(bundle.task_ids),
        ).fetchone():
            # Claim/bundle exclusion is symmetric under replay: whichever fact
            # wins event ordering owns the task, matching both live checks.
            return
        data = bundle.model_dump(mode="json")
        conn.execute(
            """INSERT INTO execution_bundles
                   (id, creation_event_id, prd_id, coordinator, status, branch, worktree_path,
                    review_policy, throughput_budget, delegated_agents,
                    checkpoint, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                bundle.id,
                bundle.creation_event_id,
                bundle.prd_id,
                bundle.coordinator,
                bundle.status.value,
                bundle.branch,
                bundle.worktree_path,
                json.dumps(data["review_policy"], sort_keys=True),
                json.dumps(data["throughput_budget"], sort_keys=True),
                json.dumps(data["delegated_agents"], sort_keys=True),
                json.dumps(data["checkpoint"], sort_keys=True)
                if data["checkpoint"] is not None
                else None,
                bundle.created_at.isoformat(),
                bundle.updated_at.isoformat(),
            ),
        )
        for position, task_id in enumerate(bundle.task_ids):
            conn.execute(
                "INSERT INTO execution_bundle_members "
                "(bundle_id, task_id, position) VALUES (?, ?, ?)",
                (bundle.id, task_id, position),
            )

    def _check_bundle_status_changed(
        self,
        conn: sqlite3.Connection,
        payload: BundleStatusChangedPayload,
        event: EventDraft,
    ) -> None:
        """Reject stale or illegal bundle lifecycle transitions before logging."""
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            raise EventRejected(
                "bundle.status_changed: event target must be bundle "
                f"'{payload.bundle_id}', got {event.target_kind} '{event.target_id}'."
            )
        if payload.changed_at != event.timestamp.astimezone(datetime.UTC):
            raise EventRejected(
                "bundle.status_changed: changed_at must match event time."
            )
        try:
            from_status = BundleStatus(payload.from_status)
            to_status = BundleStatus(payload.to_status)
        except ValueError as exc:
            raise EventRejected(f"bundle.status_changed: invalid status: {exc}") from exc
        if payload.release_claim and to_status not in TERMINAL_BUNDLE_STATUSES:
            raise EventRejected(
                "bundle.status_changed: claim release requires a terminal status."
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status, creation_event_id, updated_at "
                "FROM execution_bundles WHERE id = ?",
                (payload.bundle_id,),
            ).fetchone()
            if row is None:
                raise EventRejected(
                    f"bundle.status_changed: bundle '{payload.bundle_id}' not found."
                )
            actual = BundleStatus(row[0])
            if row[1] != payload.creation_event_id:
                raise EventRejected(
                    f"bundle.status_changed: creation event does not match "
                    f"bundle '{payload.bundle_id}'."
                )
            current_updated_at = datetime.datetime.fromisoformat(row[2])
            if payload.changed_at < current_updated_at:
                raise EventRejected(
                    f"bundle.status_changed: changed_at for '{payload.bundle_id}' "
                    "must not precede its current updated_at."
                )
            if actual is to_status:
                raise IdempotentNoOp(
                    f"bundle '{payload.bundle_id}' is already '{to_status.value}'"
                )
            if actual is not from_status:
                raise EventRejected(
                    f"bundle.status_changed: stale transition for '{payload.bundle_id}': "
                    f"expected '{from_status.value}', found '{actual.value}'."
                )
            if to_status not in _BUNDLE_TRANSITIONS[actual]:
                raise EventRejected(
                    f"bundle.status_changed: illegal transition "
                    f"'{actual.value}' -> '{to_status.value}'."
                )
            active_bundle_claim = conn.execute(
                "SELECT id, member_claim_ids, lease_expires_at FROM bundle_claims "
                "WHERE bundle_id = ? AND status = 'active'",
                (payload.bundle_id,),
            ).fetchone()
            if (
                to_status in TERMINAL_BUNDLE_STATUSES
                and active_bundle_claim is not None
                and not payload.release_claim
            ):
                raise EventRejected(
                    "bundle.status_changed: terminal transition must release the "
                    "active coordinator claim."
                )
            if to_status is BundleStatus.planned:
                coordinator = conn.execute(
                    "SELECT coordinator FROM execution_bundles WHERE id = ?",
                    (payload.bundle_id,),
                ).fetchone()[0]
                if event.actor != coordinator:
                    raise EventRejected(
                        "bundle.status_changed: only the coordinator may reopen a plan."
                    )
                overlap = conn.execute(
                    """SELECT b.id, m.task_id
                         FROM execution_bundle_members source
                         JOIN execution_bundle_members m
                           ON m.task_id = source.task_id
                          AND m.bundle_id != source.bundle_id
                         JOIN execution_bundles b ON b.id = m.bundle_id
                        WHERE source.bundle_id = ?
                          AND b.status NOT IN ('completed', 'superseded')
                        ORDER BY b.id, m.task_id
                        LIMIT 1""",
                    (payload.bundle_id,),
                ).fetchone()
                if overlap is not None:
                    raise EventRejected(
                        "bundle.status_changed: cannot reopen while replacement "
                        f"bundle '{overlap[0]}' owns member '{overlap[1]}'."
                    )
            if to_status is BundleStatus.active:
                coordinator = conn.execute(
                    "SELECT coordinator FROM execution_bundles WHERE id = ?",
                    (payload.bundle_id,),
                ).fetchone()[0]
                if event.actor != coordinator:
                    raise EventRejected(
                        "bundle.status_changed: only the coordinator may resume a bundle."
                    )
                if active_bundle_claim is None or datetime.datetime.fromisoformat(
                    active_bundle_claim[2]
                ) < event.timestamp.astimezone(datetime.UTC):
                    raise EventRejected(
                        "bundle.status_changed: active coordinator claim required."
                    )
            if active_bundle_claim is not None and (
                payload.bundle_claim_id != active_bundle_claim[0]
            ):
                raise EventRejected(
                    "bundle.status_changed: coordinator claim lineage does not match."
                )
            if to_status is BundleStatus.implemented_unreviewed:
                if active_bundle_claim is None:
                    raise EventRejected(
                        "bundle.status_changed: active coordinator claim not found."
                    )
                if datetime.datetime.fromisoformat(
                    active_bundle_claim[2]
                ) < event.timestamp.astimezone(datetime.UTC):
                    raise EventRejected(
                        "bundle.status_changed: coordinator lease has expired."
                    )
                coordinator = conn.execute(
                    "SELECT coordinator FROM execution_bundles WHERE id = ?",
                    (payload.bundle_id,),
                ).fetchone()[0]
                if event.actor != coordinator:
                    raise EventRejected(
                        "bundle.status_changed: only the coordinator may mark "
                        "a bundle implemented."
                    )
                incomplete = conn.execute(
                    """SELECT m.task_id
                         FROM execution_bundle_members m
                         JOIN tasks t ON t.id = m.task_id
                        WHERE m.bundle_id = ?
                          AND (
                            t.status NOT IN ('needs_review', 'accepted', 'done')
                            OR NOT EXISTS (
                                SELECT 1 FROM evidence e
                                 WHERE e.task_id = m.task_id
                                   AND e.category IN ('completion', 'promotion_quality')
                            )
                          )
                        ORDER BY m.position""",
                    (payload.bundle_id,),
                ).fetchall()
                if incomplete:
                    raise EventRejected(
                        "bundle.status_changed: member completion evidence is "
                        f"incomplete: {[row[0] for row in incomplete]}."
                    )
                member_claim_ids = json.loads(active_bundle_claim[1])
                wrong_lineage: list[str] = []
                for task_id, expected_claim_id in member_claim_ids.items():
                    evidence_row = conn.execute(
                        "SELECT claim_id FROM evidence WHERE task_id = ? "
                        "ORDER BY submitted_at DESC, id DESC LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    if evidence_row is None or evidence_row[0] != expected_claim_id:
                        wrong_lineage.append(task_id)
                if wrong_lineage:
                    raise EventRejected(
                        "bundle.status_changed: evidence is not bound to current "
                        f"member claims: {wrong_lineage}."
                    )
            if to_status is BundleStatus.reviewed_unintegrated:
                coordinator = conn.execute(
                    "SELECT coordinator FROM execution_bundles WHERE id = ?",
                    (payload.bundle_id,),
                ).fetchone()[0]
                if event.actor != coordinator:
                    raise EventRejected(
                        "bundle.status_changed: only the coordinator may apply "
                        "the bundle review gate."
                    )
                if active_bundle_claim is None or datetime.datetime.fromisoformat(
                    active_bundle_claim[2]
                ) < event.timestamp.astimezone(datetime.UTC):
                    raise EventRejected(
                        "bundle.status_changed: active coordinator claim required."
                    )
                if not self._bundle_review_round_passes(
                    conn, payload.bundle_id, payload.creation_event_id
                ):
                    raise EventRejected(
                        "bundle.status_changed: adversarial review quorum is incomplete."
                    )
            if to_status is BundleStatus.replan_required:
                coordinator = conn.execute(
                    "SELECT coordinator FROM execution_bundles WHERE id = ?",
                    (payload.bundle_id,),
                ).fetchone()[0]
                if event.actor != coordinator:
                    raise EventRejected(
                        "bundle.status_changed: only the coordinator may require replan."
                    )
                if from_status is BundleStatus.implemented_unreviewed:
                    if active_bundle_claim is None or datetime.datetime.fromisoformat(
                        active_bundle_claim[2]
                    ) < event.timestamp.astimezone(datetime.UTC):
                        raise EventRejected(
                            "bundle.status_changed: active coordinator claim required."
                        )
                    if not self._bundle_review_requires_replan(
                        conn, payload.bundle_id, payload.creation_event_id
                    ):
                        raise EventRejected(
                            "bundle.status_changed: review replan budget is not exhausted."
                        )
            if to_status in {
                BundleStatus.integrated,
                BundleStatus.merged,
                BundleStatus.completed,
            }:
                coordinator = conn.execute(
                    "SELECT coordinator FROM execution_bundles WHERE id = ?",
                    (payload.bundle_id,),
                ).fetchone()[0]
                if event.actor != coordinator:
                    raise EventRejected(
                        "bundle.status_changed: only the coordinator may reconcile delivery."
                    )
        finally:
            conn.execute("COMMIT")

    @staticmethod
    def _bundle_review_round_passes(
        conn: sqlite3.Connection, bundle_id: str, creation_event_id: str
    ) -> bool:
        bundle_row = conn.execute(
            "SELECT coordinator, review_policy, review_disposition_event_id "
            "FROM execution_bundles "
            "WHERE id = ? AND creation_event_id = ?",
            (bundle_id, creation_event_id),
        ).fetchone()
        if bundle_row is None or bundle_row[2] is None:
            return False
        try:
            policy = BundleReviewPolicy.model_validate(json.loads(bundle_row[1]))
        except (TypeError, ValueError):
            return False
        round_row = conn.execute(
            "SELECT MAX(review_round) FROM bundle_review_verdicts "
            "WHERE bundle_id = ? AND creation_event_id = ? "
            "AND disposition_event_id = ?",
            (bundle_id, creation_event_id, bundle_row[2]),
        ).fetchone()
        if round_row is None or round_row[0] is None:
            return False
        rows = conn.execute(
            "SELECT angle, reviewed_by, decision FROM bundle_review_verdicts "
            "WHERE bundle_id = ? AND creation_event_id = ? "
            "AND disposition_event_id = ? AND review_round = ?",
            (bundle_id, creation_event_id, bundle_row[2], round_row[0]),
        ).fetchall()
        if any(row[2] != ReviewDecision.approve.value for row in rows):
            return False
        reviewers = {row[1] for row in rows}
        angles = {row[0] for row in rows}
        required_angles = set(policy.required_angles) or {
            "correctness",
            "security",
            "integration",
        }
        if bundle_row[0] in reviewers:
            return False
        return (
            len(rows) <= max(3, policy.max_reviews)
            and len(reviewers) >= max(3, len(required_angles))
            and len(angles) >= max(3, len(required_angles))
            and required_angles.issubset(angles)
        )

    @staticmethod
    def _legacy_bundle_review_round_passes(
        conn: sqlite3.Connection, bundle_id: str, creation_event_id: str
    ) -> bool:
        """Evaluate a v12 unbound review round under the v12 gate contract."""
        if not SqliteBackend._bundle_generation_is_legacy(
            conn, creation_event_id
        ) or not SqliteBackend._bundle_disposition_is_legacy(
            conn, bundle_id, creation_event_id
        ):
            return False
        bundle_row = conn.execute(
            "SELECT coordinator, review_policy FROM execution_bundles "
            "WHERE id = ? AND creation_event_id = ?",
            (bundle_id, creation_event_id),
        ).fetchone()
        if bundle_row is None:
            return False
        try:
            policy = BundleReviewPolicy.model_validate(json.loads(bundle_row[1]))
        except (TypeError, ValueError):
            return False
        round_row = conn.execute(
            "SELECT MAX(review_round) FROM bundle_review_verdicts "
            "WHERE bundle_id = ? AND creation_event_id = ? "
            "AND disposition_event_id = 'legacy-unbound'",
            (bundle_id, creation_event_id),
        ).fetchone()
        if round_row is None or round_row[0] is None:
            return False
        rows = conn.execute(
            "SELECT angle, reviewed_by, decision FROM bundle_review_verdicts "
            "WHERE bundle_id = ? AND creation_event_id = ? "
            "AND disposition_event_id = 'legacy-unbound' AND review_round = ?",
            (bundle_id, creation_event_id, round_row[0]),
        ).fetchall()
        if any(row[2] != ReviewDecision.approve.value for row in rows):
            return False
        reviewers = {row[1] for row in rows}
        angles = {row[0] for row in rows}
        required_angles = set(policy.required_angles) or {
            "correctness",
            "security",
            "integration",
        }
        if bundle_row[0] in reviewers:
            return False
        return (
            len(rows) <= max(3, policy.max_reviews)
            and len(reviewers) >= max(3, len(required_angles))
            and len(angles) >= max(3, len(required_angles))
            and required_angles.issubset(angles)
        )

    @staticmethod
    def _legacy_bundle_review_requires_replan(
        conn: sqlite3.Connection, bundle_id: str, creation_event_id: str
    ) -> bool:
        if not SqliteBackend._bundle_generation_is_legacy(
            conn, creation_event_id
        ) or not SqliteBackend._bundle_disposition_is_legacy(
            conn, bundle_id, creation_event_id
        ):
            return False
        row = conn.execute(
            "SELECT coordinator, review_policy FROM execution_bundles "
            "WHERE id = ? AND creation_event_id = ?",
            (bundle_id, creation_event_id),
        ).fetchone()
        if row is None:
            return False
        try:
            policy = BundleReviewPolicy.model_validate(json.loads(row[1]))
        except (TypeError, ValueError):
            return False
        round_row = conn.execute(
            "SELECT MAX(review_round) FROM bundle_review_verdicts "
            "WHERE bundle_id = ? AND creation_event_id = ? "
            "AND disposition_event_id = 'legacy-unbound'",
            (bundle_id, creation_event_id),
        ).fetchone()
        if round_row is None or round_row[0] is None:
            return False
        review_round = int(round_row[0])
        return (
            review_round - 1 >= policy.max_rereviews
            and SqliteBackend._bundle_review_round_complete_with_blocker(
                conn,
                bundle_id,
                creation_event_id,
                "legacy-unbound",
                review_round,
                policy,
                row[0],
            )
        )

    @staticmethod
    def _bundle_review_round_complete_with_blocker(
        conn: sqlite3.Connection,
        bundle_id: str,
        creation_event_id: str,
        disposition_event_id: str,
        review_round: int,
        policy: BundleReviewPolicy,
        coordinator: str,
    ) -> bool:
        rows = conn.execute(
            "SELECT angle, reviewed_by, decision FROM bundle_review_verdicts "
            "WHERE bundle_id = ? AND creation_event_id = ? "
            "AND disposition_event_id = ? AND review_round = ?",
            (bundle_id, creation_event_id, disposition_event_id, review_round),
        ).fetchall()
        reviewers = {row[1] for row in rows}
        angles = {row[0] for row in rows}
        required_angles = set(policy.required_angles) or {
            "correctness",
            "security",
            "integration",
        }
        required = max(3, len(required_angles))
        return (
            coordinator not in reviewers
            and len(rows) <= max(3, policy.max_reviews)
            and len(reviewers) >= required
            and len(angles) >= required
            and required_angles.issubset(angles)
            and any(row[2] != ReviewDecision.approve.value for row in rows)
        )

    @staticmethod
    def _bundle_review_requires_replan(
        conn: sqlite3.Connection, bundle_id: str, creation_event_id: str
    ) -> bool:
        row = conn.execute(
            "SELECT coordinator, review_policy, review_disposition_event_id "
            "FROM execution_bundles "
            "WHERE id = ? AND creation_event_id = ?",
            (bundle_id, creation_event_id),
        ).fetchone()
        if row is None or row[2] is None:
            return False
        try:
            policy = BundleReviewPolicy.model_validate(json.loads(row[1]))
        except (TypeError, ValueError):
            return False
        round_row = conn.execute(
            "SELECT MAX(review_round) FROM bundle_review_verdicts "
            "WHERE bundle_id = ? AND creation_event_id = ? "
            "AND disposition_event_id = ?",
            (bundle_id, creation_event_id, row[2]),
        ).fetchone()
        if round_row is None or round_row[0] is None:
            return False
        review_round = int(round_row[0])
        return (
            review_round - 1 >= policy.max_rereviews
            and SqliteBackend._bundle_review_round_complete_with_blocker(
                conn,
                bundle_id,
                creation_event_id,
                row[2],
                review_round,
                policy,
                row[0],
            )
        )

    @staticmethod
    def _write_bundle_status_changed(
        conn: sqlite3.Connection,
        payload: BundleStatusChangedPayload,
        event: Event,
    ) -> None:
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            return
        if payload.changed_at != event.timestamp.astimezone(datetime.UTC):
            return
        if payload.to_status not in _BUNDLE_TRANSITIONS.get(
            payload.from_status, frozenset()
        ):
            return
        if payload.release_claim and payload.to_status not in TERMINAL_BUNDLE_STATUSES:
            return
        if payload.to_status is BundleStatus.planned:
            coordinator = conn.execute(
                "SELECT coordinator FROM execution_bundles WHERE id = ? "
                "AND creation_event_id = ?",
                (payload.bundle_id, payload.creation_event_id),
            ).fetchone()
            if coordinator is None or event.actor != coordinator[0]:
                return
            if conn.execute(
                """SELECT 1
                     FROM execution_bundle_members source
                     JOIN execution_bundle_members other
                       ON other.task_id = source.task_id
                      AND other.bundle_id != source.bundle_id
                     JOIN execution_bundles b ON b.id = other.bundle_id
                    WHERE source.bundle_id = ?
                      AND b.status NOT IN ('completed', 'superseded')
                    LIMIT 1""",
                (payload.bundle_id,),
            ).fetchone():
                return
        if payload.to_status in {
            BundleStatus.reviewed_unintegrated,
            BundleStatus.replan_required,
            BundleStatus.integrated,
            BundleStatus.merged,
            BundleStatus.completed,
        }:
            coordinator = conn.execute(
                "SELECT coordinator FROM execution_bundles WHERE id = ? "
                "AND creation_event_id = ?",
                (payload.bundle_id, payload.creation_event_id),
            ).fetchone()
            if coordinator is None or event.actor != coordinator[0]:
                return
            if (
                payload.to_status is BundleStatus.reviewed_unintegrated
                and not SqliteBackend._bundle_review_round_passes(
                    conn, payload.bundle_id, payload.creation_event_id
                )
                and not SqliteBackend._legacy_bundle_review_round_passes(
                    conn, payload.bundle_id, payload.creation_event_id
                )
            ):
                return
            if (
                payload.to_status is BundleStatus.replan_required
                and payload.from_status is BundleStatus.implemented_unreviewed
                and not SqliteBackend._bundle_review_requires_replan(
                    conn, payload.bundle_id, payload.creation_event_id
                )
                and not SqliteBackend._legacy_bundle_review_requires_replan(
                    conn, payload.bundle_id, payload.creation_event_id
                )
            ):
                return
        active_claim = conn.execute(
            "SELECT id, lease_expires_at, member_claim_ids FROM bundle_claims "
            "WHERE bundle_id = ? AND status = 'active'",
            (payload.bundle_id,),
        ).fetchone()
        if payload.to_status is BundleStatus.active:
            coordinator = conn.execute(
                "SELECT coordinator FROM execution_bundles WHERE id = ? "
                "AND creation_event_id = ?",
                (payload.bundle_id, payload.creation_event_id),
            ).fetchone()
            if (
                coordinator is None
                or event.actor != coordinator[0]
                or active_claim is None
                or datetime.datetime.fromisoformat(active_claim[1])
                < event.timestamp.astimezone(datetime.UTC)
            ):
                return
        if payload.to_status in {
            BundleStatus.reviewed_unintegrated,
            BundleStatus.replan_required,
        } and payload.from_status is BundleStatus.implemented_unreviewed:
            if active_claim is None or datetime.datetime.fromisoformat(
                active_claim[1]
            ) < event.timestamp.astimezone(datetime.UTC):
                return
        if payload.to_status is BundleStatus.implemented_unreviewed:
            coordinator = conn.execute(
                "SELECT coordinator FROM execution_bundles WHERE id = ? "
                "AND creation_event_id = ?",
                (payload.bundle_id, payload.creation_event_id),
            ).fetchone()
            if (
                coordinator is None
                or event.actor != coordinator[0]
                or active_claim is None
                or datetime.datetime.fromisoformat(active_claim[1])
                < event.timestamp.astimezone(datetime.UTC)
            ):
                return
            incomplete = conn.execute(
                """SELECT m.task_id
                     FROM execution_bundle_members m
                     JOIN tasks t ON t.id = m.task_id
                    WHERE m.bundle_id = ?
                      AND (
                        t.status NOT IN ('needs_review', 'accepted', 'done')
                        OR NOT EXISTS (
                            SELECT 1 FROM evidence e
                             WHERE e.task_id = m.task_id
                               AND e.category IN ('completion', 'promotion_quality')
                        )
                      )""",
                (payload.bundle_id,),
            ).fetchall()
            if incomplete:
                return
            for task_id, expected_claim_id in json.loads(active_claim[2]).items():
                evidence_row = conn.execute(
                    "SELECT claim_id FROM evidence WHERE task_id = ? "
                    "ORDER BY submitted_at DESC, id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if evidence_row is None or evidence_row[0] != expected_claim_id:
                    return
        if active_claim is not None and payload.bundle_claim_id != active_claim[0]:
            return
        disposition_event_id = (
            event.id
            if payload.to_status is BundleStatus.implemented_unreviewed
            else None
        )
        last_result_at = (
            payload.changed_at.isoformat()
            if payload.to_status
            in {
                BundleStatus.reviewed_unintegrated,
                BundleStatus.integrated,
                BundleStatus.merged,
                BundleStatus.completed,
            }
            else None
        )
        cursor = conn.execute(
            "UPDATE execution_bundles SET status = ?, updated_at = ?, "
            "review_disposition_event_id = COALESCE(?, review_disposition_event_id), "
            "last_result_at = COALESCE(?, last_result_at) "
            "WHERE id = ? AND creation_event_id = ? AND status = ? "
            "AND updated_at <= ?",
            (
                payload.to_status,
                payload.changed_at.isoformat(),
                disposition_event_id,
                last_result_at,
                payload.bundle_id,
                payload.creation_event_id,
                payload.from_status,
                payload.changed_at.isoformat(),
            ),
        )
        if cursor.rowcount != 1:
            return
        if payload.release_claim and active_claim is not None:
            SqliteBackend._write_bundle_claim_terminal(
                conn,
                bundle_claim_id=active_claim[0],
                bundle_id=payload.bundle_id,
                terminal_status="released",
                timestamp=payload.changed_at.isoformat(),
                reason=f"bundle reached {payload.to_status.value}",
            )

    @staticmethod
    def _check_bundle_agent_observed(
        conn: sqlite3.Connection,
        payload: BundleAgentObservedPayload,
        event: EventDraft,
    ) -> None:
        """Validate observation ownership only; handle state never gates lifecycle."""
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            raise EventRejected(
                "bundle.agent_observed: event target must be bundle "
                f"'{payload.bundle_id}', got {event.target_kind} '{event.target_id}'."
            )
        if not payload.metadata_only:
            raise EventRejected(
                "bundle.agent_observed: metadata_only=true is required for live append."
            )
        if payload.observation.observed_at != event.timestamp.astimezone(datetime.UTC):
            raise EventRejected(
                "bundle.agent_observed: observed_at must match event time."
            )
        row = conn.execute(
            "SELECT creation_event_id, coordinator FROM execution_bundles WHERE id = ?",
            (payload.bundle_id,),
        ).fetchone()
        if row is None:
            raise EventRejected(
                f"bundle.agent_observed: bundle '{payload.bundle_id}' not found."
            )
        if row[0] != payload.creation_event_id:
            raise EventRejected(
                f"bundle.agent_observed: creation event does not match "
                f"bundle '{payload.bundle_id}'."
            )
        if row[1] != event.actor:
            raise EventRejected(
                "bundle.agent_observed: only the coordinator may record observations."
            )
        member_rows = conn.execute(
            "SELECT task_id FROM execution_bundle_members WHERE bundle_id = ?",
            (payload.bundle_id,),
        ).fetchall()
        members = {row[0] for row in member_rows}
        outside = [task_id for task_id in payload.observation.task_ids if task_id not in members]
        if outside:
            raise EventRejected(
                f"bundle.agent_observed: observation references non-member tasks: {outside}."
            )

    @staticmethod
    def _write_bundle_agent_observed(
        conn: sqlite3.Connection,
        payload: BundleAgentObservedPayload,
        event: Event,
    ) -> None:
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            return
        row = conn.execute(
            "SELECT delegated_agents, coordinator, updated_at FROM execution_bundles "
            "WHERE id = ? AND creation_event_id = ?",
            (payload.bundle_id, payload.creation_event_id),
        ).fetchone()
        if row is None:
            return
        legacy_generation = SqliteBackend._bundle_generation_is_legacy(
            conn, payload.creation_event_id
        )
        if not payload.metadata_only and not legacy_generation:
            return
        if payload.metadata_only and (
            row[1] != event.actor
            or payload.observation.observed_at
            != event.timestamp.astimezone(datetime.UTC)
        ):
            return
        members = {
            member[0]
            for member in conn.execute(
                "SELECT task_id FROM execution_bundle_members WHERE bundle_id = ?",
                (payload.bundle_id,),
            ).fetchall()
        }
        if any(task_id not in members for task_id in payload.observation.task_ids):
            return
        observations = json.loads(row[0] or "[]") if row is not None else []
        new_observation = payload.observation.model_dump(mode="json")
        previous = next(
            (
                item
                for item in observations
                if item.get("id") == payload.observation.id
            ),
            None,
        )
        if previous is not None:
            previous_observed_at = datetime.datetime.fromisoformat(
                previous["observed_at"]
            )
            if previous_observed_at >= payload.observation.observed_at:
                return
        observations = [
            item for item in observations if item.get("id") != payload.observation.id
        ]
        observations.append(new_observation)
        observations.sort(key=lambda item: item["id"])
        projected_updated_at = datetime.datetime.fromisoformat(row[2])
        if not payload.metadata_only:
            projected_updated_at = max(projected_updated_at, event.timestamp)
        conn.execute(
            "UPDATE execution_bundles SET delegated_agents = ?, updated_at = ? "
            "WHERE id = ? AND creation_event_id = ?",
            (
                json.dumps(observations, sort_keys=True),
                projected_updated_at.isoformat(),
                payload.bundle_id,
                payload.creation_event_id,
            ),
        )

    @staticmethod
    def _check_bundle_review_recorded(
        conn: sqlite3.Connection,
        payload: BundleReviewRecordedPayload,
        event: EventDraft,
    ) -> None:
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            raise EventRejected("bundle.review_recorded: event target mismatch.")
        if event.actor != payload.reviewed_by:
            raise EventRejected("bundle.review_recorded: event actor mismatch.")
        if payload.created_at != event.timestamp.astimezone(datetime.UTC):
            raise EventRejected("bundle.review_recorded: created_at must match event time.")
        if payload.disposition_event_id is None:
            raise EventRejected(
                "bundle.review_recorded: disposition_event_id is required for live append."
            )
        row = conn.execute(
            "SELECT creation_event_id, coordinator, status, review_policy, "
            "review_disposition_event_id, updated_at "
            "FROM execution_bundles WHERE id = ?",
            (payload.bundle_id,),
        ).fetchone()
        if row is None or row[0] != payload.creation_event_id:
            raise EventRejected("bundle.review_recorded: bundle generation not found.")
        if row[4] != payload.disposition_event_id:
            raise EventRejected("bundle.review_recorded: stale review disposition.")
        if row[2] != BundleStatus.implemented_unreviewed.value:
            raise EventRejected(
                "bundle.review_recorded: bundle must be implemented_unreviewed."
            )
        event_time = event.timestamp.astimezone(datetime.UTC)
        if datetime.datetime.fromisoformat(row[5]) > event_time:
            raise EventRejected("bundle.review_recorded: review predates bundle state.")
        policy = BundleReviewPolicy.model_validate(json.loads(row[3]))
        if payload.reviewed_by == row[1]:
            raise EventRejected("bundle.review_recorded: coordinator cannot self-review.")
        active_claim = conn.execute(
            "SELECT lease_expires_at, created_at, last_heartbeat_at "
            "FROM bundle_claims WHERE bundle_id = ? AND status = 'active'",
            (payload.bundle_id,),
        ).fetchone()
        if (
            active_claim is None
            or datetime.datetime.fromisoformat(active_claim[0]) < event_time
            or max(
                datetime.datetime.fromisoformat(active_claim[1]),
                datetime.datetime.fromisoformat(active_claim[2]),
            )
            > event_time
        ):
            raise EventRejected(
                "bundle.review_recorded: active coordinator claim required."
            )
        if payload.review_round > policy.max_rereviews + 1:
            raise EventRejected("bundle.review_recorded: re-review budget exhausted.")
        if payload.decision is not ReviewDecision.approve and not (
            payload.notes and payload.notes.strip()
        ):
            raise EventRejected("bundle.review_recorded: blocking verdict needs notes.")
        existing_id = conn.execute(
            "SELECT 1 FROM bundle_review_verdicts WHERE id = ?", (payload.id,)
        ).fetchone()
        if existing_id is not None:
            raise EventRejected(f"bundle.review_recorded: id '{payload.id}' already exists.")
        duplicate_reviewer = conn.execute(
            "SELECT 1 FROM bundle_review_verdicts WHERE bundle_id = ? "
            "AND creation_event_id = ? AND disposition_event_id = ? "
            "AND review_round = ? AND reviewed_by = ?",
            (
                payload.bundle_id,
                payload.creation_event_id,
                payload.disposition_event_id,
                payload.review_round,
                payload.reviewed_by,
            ),
        ).fetchone()
        if duplicate_reviewer is not None:
            raise EventRejected(
                "bundle.review_recorded: reviewer already submitted this round."
            )
        round_count = conn.execute(
            "SELECT COUNT(*) FROM bundle_review_verdicts WHERE bundle_id = ? "
            "AND creation_event_id = ? AND disposition_event_id = ? "
            "AND review_round = ?",
            (
                payload.bundle_id,
                payload.creation_event_id,
                payload.disposition_event_id,
                payload.review_round,
            ),
        ).fetchone()[0]
        if round_count >= max(3, policy.max_reviews):
            raise EventRejected("bundle.review_recorded: review cap reached.")
        max_round = conn.execute(
            "SELECT COALESCE(MAX(review_round), 0) FROM bundle_review_verdicts "
            "WHERE bundle_id = ? AND creation_event_id = ? "
            "AND disposition_event_id = ?",
            (
                payload.bundle_id,
                payload.creation_event_id,
                payload.disposition_event_id,
            ),
        ).fetchone()[0]
        if payload.review_round > max_round + 1:
            raise EventRejected("bundle.review_recorded: review rounds must be contiguous.")
        if payload.review_round == max_round + 1 and max_round > 0 and not (
            SqliteBackend._bundle_review_round_complete_with_blocker(
                conn,
                payload.bundle_id,
                payload.creation_event_id,
                payload.disposition_event_id,
                max_round,
                policy,
                row[1],
            )
        ):
            raise EventRejected(
                "bundle.review_recorded: prior round is not a complete blocking quorum."
            )

    @staticmethod
    def _check_bundle_checkpoint_recorded(
        conn: sqlite3.Connection,
        payload: BundleCheckpointRecordedPayload,
        event: EventDraft,
    ) -> None:
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            raise EventRejected("bundle.checkpoint_recorded: event target mismatch.")
        if event.actor != payload.checkpoint.recorded_by:
            raise EventRejected("bundle.checkpoint_recorded: event actor mismatch.")
        if payload.checkpoint.recorded_at != event.timestamp.astimezone(datetime.UTC):
            raise EventRejected(
                "bundle.checkpoint_recorded: recorded_at must match event time."
            )
        row = conn.execute(
            "SELECT creation_event_id, status, checkpoint, coordinator, updated_at "
            "FROM execution_bundles "
            "WHERE id = ?",
            (payload.bundle_id,),
        ).fetchone()
        if row is None or row[0] != payload.creation_event_id:
            raise EventRejected("bundle.checkpoint_recorded: bundle generation not found.")
        if row[1] == BundleStatus.superseded.value:
            raise EventRejected("bundle.checkpoint_recorded: bundle is superseded.")
        if row[3] != payload.checkpoint.recorded_by:
            raise EventRejected(
                "bundle.checkpoint_recorded: only the coordinator may checkpoint."
            )
        if datetime.datetime.fromisoformat(row[4]) > event.timestamp.astimezone(
            datetime.UTC
        ):
            raise EventRejected(
                "bundle.checkpoint_recorded: checkpoint predates bundle state."
            )
        if row[2]:
            existing = json.loads(row[2])
            if (
                existing.get("commit_sha") == payload.checkpoint.commit_sha
                and existing.get("pr_url") == payload.checkpoint.pr_url
            ):
                raise IdempotentNoOp("bundle checkpoint is already recorded")

    @staticmethod
    def _write_bundle_checkpoint_recorded(
        conn: sqlite3.Connection,
        payload: BundleCheckpointRecordedPayload,
        event: Event,
    ) -> None:
        row = conn.execute(
            "SELECT creation_event_id, status, updated_at, checkpoint, coordinator "
            "FROM execution_bundles "
            "WHERE id = ?",
            (payload.bundle_id,),
        ).fetchone()
        event_time = event.timestamp.astimezone(datetime.UTC)
        if (
            event.target_kind != "bundle"
            or event.target_id != payload.bundle_id
            or event.actor != payload.checkpoint.recorded_by
            or payload.checkpoint.recorded_at != event_time
            or row is None
            or row[0] != payload.creation_event_id
            or row[1] == BundleStatus.superseded.value
            or row[4] != payload.checkpoint.recorded_by
            or datetime.datetime.fromisoformat(row[2]) > event_time
        ):
            return
        if row[3]:
            existing = json.loads(row[3])
            if (
                existing.get("commit_sha") == payload.checkpoint.commit_sha
                and existing.get("pr_url") == payload.checkpoint.pr_url
            ):
                return
        conn.execute(
            "UPDATE execution_bundles SET checkpoint = ?, updated_at = ? "
            "WHERE id = ? AND creation_event_id = ?",
            (
                json.dumps(payload.checkpoint.model_dump(mode="json"), sort_keys=True),
                event_time.isoformat(),
                payload.bundle_id,
                payload.creation_event_id,
            ),
        )

    @staticmethod
    def _check_bundle_superseded(
        conn: sqlite3.Connection,
        payload: BundleSupersededPayload,
        event: EventDraft,
    ) -> None:
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            raise EventRejected("bundle.superseded: event target mismatch.")
        if event.actor != payload.superseded_by_actor:
            raise EventRejected("bundle.superseded: event actor mismatch.")
        if payload.superseded_at != event.timestamp.astimezone(datetime.UTC):
            raise EventRejected("bundle.superseded: superseded_at must match event time.")
        if payload.bundle_id == payload.replacement_bundle_id:
            raise EventRejected("bundle.superseded: replacement must be another bundle.")
        source = conn.execute(
            "SELECT creation_event_id, prd_id, status, coordinator, updated_at "
            "FROM execution_bundles WHERE id = ?",
            (payload.bundle_id,),
        ).fetchone()
        replacement = conn.execute(
            "SELECT prd_id, status, superseded_by, created_at, updated_at "
            "FROM execution_bundles WHERE id = ?",
            (payload.replacement_bundle_id,),
        ).fetchone()
        if source is None or source[0] != payload.creation_event_id:
            raise EventRejected("bundle.superseded: source generation not found.")
        if source[3] != payload.superseded_by_actor:
            raise EventRejected("bundle.superseded: only the coordinator may supersede.")
        if source[2] in {status.value for status in TERMINAL_BUNDLE_STATUSES}:
            raise EventRejected("bundle.superseded: source is terminal.")
        if datetime.datetime.fromisoformat(source[4]) > event.timestamp.astimezone(
            datetime.UTC
        ):
            raise EventRejected("bundle.superseded: supersession predates source state.")
        if replacement is None or replacement[0] != source[1]:
            raise EventRejected("bundle.superseded: replacement must exist in the same PRD.")
        if replacement[1] in {status.value for status in TERMINAL_BUNDLE_STATUSES}:
            raise EventRejected("bundle.superseded: replacement is terminal.")
        if max(
            datetime.datetime.fromisoformat(replacement[3]),
            datetime.datetime.fromisoformat(replacement[4]),
        ) > event.timestamp.astimezone(datetime.UTC):
            raise EventRejected("bundle.superseded: supersession predates replacement state.")

    @staticmethod
    def _write_bundle_superseded(
        conn: sqlite3.Connection,
        payload: BundleSupersededPayload,
        event: Event,
    ) -> None:
        source = conn.execute(
            "SELECT creation_event_id, prd_id, status, coordinator, updated_at "
            "FROM execution_bundles "
            "WHERE id = ?",
            (payload.bundle_id,),
        ).fetchone()
        replacement = conn.execute(
            "SELECT prd_id, status, created_at, updated_at "
            "FROM execution_bundles WHERE id = ?",
            (payload.replacement_bundle_id,),
        ).fetchone()
        event_time = event.timestamp.astimezone(datetime.UTC)
        if (
            event.target_kind != "bundle"
            or event.target_id != payload.bundle_id
            or event.actor != payload.superseded_by_actor
            or payload.superseded_at != event_time
            or payload.bundle_id == payload.replacement_bundle_id
            or source is None
            or source[0] != payload.creation_event_id
            or source[3] != payload.superseded_by_actor
            or source[2] in {status.value for status in TERMINAL_BUNDLE_STATUSES}
            or datetime.datetime.fromisoformat(source[4]) > event_time
            or replacement is None
            or replacement[0] != source[1]
            or replacement[1] in {
                status.value for status in TERMINAL_BUNDLE_STATUSES
            }
            or max(
                datetime.datetime.fromisoformat(replacement[2]),
                datetime.datetime.fromisoformat(replacement[3]),
            )
            > event_time
        ):
            return
        active_claim = conn.execute(
            "SELECT id FROM bundle_claims WHERE bundle_id = ? AND status = 'active'",
            (payload.bundle_id,),
        ).fetchone()
        if active_claim is not None:
            SqliteBackend._write_bundle_claim_terminal(
                conn,
                bundle_claim_id=active_claim[0],
                bundle_id=payload.bundle_id,
                terminal_status="released",
                timestamp=event_time.isoformat(),
                reason=f"superseded by {payload.replacement_bundle_id}",
            )
        # Reopen only when the latest evidence belongs to the source
        # generation. A replacement may already have submitted newer evidence
        # before the redirect is recorded; never rewind that replacement state.
        conn.execute(
            """UPDATE tasks SET status = 'ready', updated_at = ?
                 WHERE status = 'needs_review'
                   AND id IN (
                       SELECT source.task_id
                         FROM execution_bundle_members source
                         JOIN execution_bundle_members replacement
                           ON replacement.task_id = source.task_id
                        WHERE source.bundle_id = ?
                          AND replacement.bundle_id = ?
                   )
                   AND (
                       SELECT c.bundle_claim_id
                         FROM evidence e
                         JOIN claims c ON c.id = e.claim_id
                        WHERE e.task_id = tasks.id
                        ORDER BY e.submitted_at DESC, e.id DESC
                        LIMIT 1
                   ) IN (
                       SELECT id FROM bundle_claims WHERE bundle_id = ?
                   )""",
            (
                event_time.isoformat(),
                payload.bundle_id,
                payload.replacement_bundle_id,
                payload.bundle_id,
            ),
        )
        conn.execute(
            "UPDATE execution_bundles SET status = 'superseded', superseded_by = ?, "
            "updated_at = ? WHERE id = ? AND creation_event_id = ?",
            (
                payload.replacement_bundle_id,
                event_time.isoformat(),
                payload.bundle_id,
                payload.creation_event_id,
            ),
        )

    @staticmethod
    def _check_bundle_plan_acknowledged(
        conn: sqlite3.Connection,
        payload: BundlePlanAcknowledgedPayload,
        event: EventDraft,
    ) -> None:
        if event.target_kind != "prd" or event.target_id != payload.prd_id:
            raise EventRejected("bundle.plan_acknowledged: event target mismatch.")
        if event.actor != payload.acknowledged_by:
            raise EventRejected("bundle.plan_acknowledged: event actor mismatch.")
        if payload.created_at != event.timestamp.astimezone(datetime.UTC):
            raise EventRejected(
                "bundle.plan_acknowledged: created_at must match event time."
            )
        if conn.execute(
            "SELECT 1 FROM prds WHERE id = ?", (payload.prd_id,)
        ).fetchone() is None:
            raise EventRejected("bundle.plan_acknowledged: PRD not found.")

    @staticmethod
    def _write_legacy_bundle_review_recorded(
        conn: sqlite3.Connection,
        payload: BundleReviewRecordedPayload,
        event: Event,
    ) -> None:
        """Project a canonical v12 verdict under the exact v12 acceptance rules."""
        if not SqliteBackend._bundle_generation_is_legacy(
            conn, payload.creation_event_id
        ):
            return
        row = conn.execute(
            "SELECT creation_event_id, coordinator, status, review_policy "
            "FROM execution_bundles WHERE id = ?",
            (payload.bundle_id,),
        ).fetchone()
        if (
            event.target_kind != "bundle"
            or event.target_id != payload.bundle_id
            or event.actor != payload.reviewed_by
            or payload.created_at != event.timestamp.astimezone(datetime.UTC)
            or row is None
            or row[0] != payload.creation_event_id
            or row[2] != BundleStatus.implemented_unreviewed.value
        ):
            return
        try:
            policy = BundleReviewPolicy.model_validate(json.loads(row[3]))
        except (TypeError, ValueError):
            return
        if (
            payload.reviewed_by == row[1]
            or payload.review_round > policy.max_rereviews + 1
            or (
                payload.decision is not ReviewDecision.approve
                and not (payload.notes and payload.notes.strip())
            )
        ):
            return
        active_claim = conn.execute(
            "SELECT lease_expires_at FROM bundle_claims WHERE bundle_id = ? "
            "AND status = 'active'",
            (payload.bundle_id,),
        ).fetchone()
        if active_claim is None or datetime.datetime.fromisoformat(
            active_claim[0]
        ) < event.timestamp.astimezone(datetime.UTC):
            return
        max_round = conn.execute(
            "SELECT COALESCE(MAX(review_round), 0) FROM bundle_review_verdicts "
            "WHERE bundle_id = ? AND creation_event_id = ? "
            "AND disposition_event_id = 'legacy-unbound'",
            (payload.bundle_id, payload.creation_event_id),
        ).fetchone()[0]
        if payload.review_round > max_round + 1:
            return
        if payload.review_round == max_round + 1 and max_round > 0 and not (
            SqliteBackend._bundle_review_round_complete_with_blocker(
                conn,
                payload.bundle_id,
                payload.creation_event_id,
                "legacy-unbound",
                max_round,
                policy,
                row[1],
            )
        ):
            return
        duplicate = conn.execute(
            "SELECT 1 FROM bundle_review_verdicts WHERE id = ? OR "
            "(bundle_id = ? AND creation_event_id = ? "
            "AND disposition_event_id = 'legacy-unbound' "
            "AND review_round = ? AND reviewed_by = ?)",
            (
                payload.id,
                payload.bundle_id,
                payload.creation_event_id,
                payload.review_round,
                payload.reviewed_by,
            ),
        ).fetchone()
        if duplicate is not None:
            return
        round_count = conn.execute(
            "SELECT COUNT(*) FROM bundle_review_verdicts WHERE bundle_id = ? "
            "AND creation_event_id = ? AND disposition_event_id = 'legacy-unbound' "
            "AND review_round = ?",
            (
                payload.bundle_id,
                payload.creation_event_id,
                payload.review_round,
            ),
        ).fetchone()[0]
        if round_count >= max(3, policy.max_reviews):
            return
        conn.execute(
            "INSERT INTO bundle_review_verdicts "
            "(id, bundle_id, creation_event_id, disposition_event_id, "
            "review_round, angle, reviewed_by, decision, notes, created_at) "
            "VALUES (?, ?, ?, 'legacy-unbound', ?, ?, ?, ?, ?, ?)",
            (
                payload.id,
                payload.bundle_id,
                payload.creation_event_id,
                payload.review_round,
                payload.angle.strip().lower(),
                payload.reviewed_by,
                payload.decision.value,
                payload.notes,
                payload.created_at.isoformat(),
            ),
        )

    @staticmethod
    def _write_bundle_review_recorded(
        conn: sqlite3.Connection,
        payload: BundleReviewRecordedPayload,
        event: Event,
    ) -> None:
        if payload.disposition_event_id is None:
            SqliteBackend._write_legacy_bundle_review_recorded(conn, payload, event)
            return
        row = conn.execute(
            "SELECT creation_event_id, coordinator, status, review_policy, "
            "review_disposition_event_id, updated_at "
            "FROM execution_bundles WHERE id = ?",
            (payload.bundle_id,),
        ).fetchone()
        disposition_event_id = payload.disposition_event_id or "legacy-unbound"
        event_time = event.timestamp.astimezone(datetime.UTC)
        if (
            event.target_kind != "bundle"
            or event.target_id != payload.bundle_id
            or event.actor != payload.reviewed_by
            or payload.created_at != event_time
            or row is None
            or row[0] != payload.creation_event_id
            or (
                payload.disposition_event_id is not None
                and row[4] != payload.disposition_event_id
            )
            or row[2] != BundleStatus.implemented_unreviewed.value
            or datetime.datetime.fromisoformat(row[5]) > event_time
        ):
            return
        try:
            policy = BundleReviewPolicy.model_validate(json.loads(row[3]))
        except (TypeError, ValueError):
            return
        if (
            payload.reviewed_by == row[1]
            or payload.review_round > policy.max_rereviews + 1
            or (
                payload.decision is not ReviewDecision.approve
                and not (payload.notes and payload.notes.strip())
            )
        ):
            return
        active_claim = conn.execute(
            "SELECT lease_expires_at, created_at, last_heartbeat_at "
            "FROM bundle_claims WHERE bundle_id = ? AND status = 'active'",
            (payload.bundle_id,),
        ).fetchone()
        if (
            active_claim is None
            or datetime.datetime.fromisoformat(active_claim[0]) < event_time
            or max(
                datetime.datetime.fromisoformat(active_claim[1]),
                datetime.datetime.fromisoformat(active_claim[2]),
            )
            > event_time
        ):
            return
        max_round = conn.execute(
            "SELECT COALESCE(MAX(review_round), 0) FROM bundle_review_verdicts "
            "WHERE bundle_id = ? AND creation_event_id = ? "
            "AND disposition_event_id = ?",
            (
                payload.bundle_id,
                payload.creation_event_id,
                disposition_event_id,
            ),
        ).fetchone()[0]
        if payload.review_round > max_round + 1:
            return
        if payload.review_round == max_round + 1 and max_round > 0 and not (
            SqliteBackend._bundle_review_round_complete_with_blocker(
                conn,
                payload.bundle_id,
                payload.creation_event_id,
                disposition_event_id,
                max_round,
                policy,
                row[1],
            )
        ):
            return
        duplicate = conn.execute(
            "SELECT 1 FROM bundle_review_verdicts WHERE id = ? OR "
            "(bundle_id = ? AND creation_event_id = ? AND disposition_event_id = ? "
            "AND review_round = ? "
            "AND reviewed_by = ?)",
            (
                payload.id,
                payload.bundle_id,
                payload.creation_event_id,
                disposition_event_id,
                payload.review_round,
                payload.reviewed_by,
            ),
        ).fetchone()
        if duplicate is not None:
            return
        round_count = conn.execute(
            "SELECT COUNT(*) FROM bundle_review_verdicts WHERE bundle_id = ? "
            "AND creation_event_id = ? AND disposition_event_id = ? "
            "AND review_round = ?",
            (
                payload.bundle_id,
                payload.creation_event_id,
                disposition_event_id,
                payload.review_round,
            ),
        ).fetchone()[0]
        if round_count >= max(3, policy.max_reviews):
            return
        conn.execute(
            "INSERT INTO bundle_review_verdicts "
            "(id, bundle_id, creation_event_id, disposition_event_id, "
            "review_round, angle, reviewed_by, decision, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload.id,
                payload.bundle_id,
                payload.creation_event_id,
                disposition_event_id,
                payload.review_round,
                payload.angle.strip().lower(),
                payload.reviewed_by,
                payload.decision.value,
                payload.notes,
                payload.created_at.isoformat(),
            ),
        )

    def _check_bundle_claimed(
        self,
        conn: sqlite3.Connection,
        payload: BundleClaimedPayload,
        event: EventDraft,
    ) -> None:
        """Validate the complete coordinator reservation on a fresh snapshot."""
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            raise EventRejected(
                f"bundle.claimed: event target must be bundle '{payload.bundle_id}'."
            )
        if event.actor != payload.claimed_by:
            raise EventRejected("bundle.claimed: event actor must be the coordinator.")
        event_time = event.timestamp.astimezone(datetime.UTC)
        if payload.created_at != event_time or payload.last_heartbeat_at != event_time:
            raise EventRejected(
                "bundle.claimed: creation and heartbeat must match the event timestamp."
            )
        if payload.lease_expires_at <= event_time:
            raise EventRejected("bundle.claimed: lease must expire in the future.")
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT creation_event_id, coordinator, status, throughput_budget, "
                "created_at, updated_at "
                "FROM execution_bundles WHERE id = ?",
                (payload.bundle_id,),
            ).fetchone()
            if row is None:
                raise EventRejected(
                    f"bundle.claimed: bundle '{payload.bundle_id}' not found."
                )
            if row[0] != payload.creation_event_id:
                raise EventRejected("bundle.claimed: creation event does not match.")
            if row[1] != payload.claimed_by:
                raise EventRejected(
                    f"bundle.claimed: coordinator is '{row[1]}', not "
                    f"'{payload.claimed_by}'."
                )
            if row[2] != BundleStatus.planned.value:
                raise EventRejected(
                    f"bundle.claimed: bundle status is '{row[2]}', expected 'planned'."
                )
            if max(
                datetime.datetime.fromisoformat(row[4]),
                datetime.datetime.fromisoformat(row[5]),
            ) > event_time:
                raise EventRejected("bundle.claimed: claim predates bundle state.")
            if conn.execute(
                "SELECT 1 FROM bundle_claims WHERE bundle_id = ? AND status = 'active'",
                (payload.bundle_id,),
            ).fetchone():
                raise EventRejected("bundle.claimed: bundle already has a claim.")
            if conn.execute(
                "SELECT 1 FROM bundle_claims WHERE id = ?", (payload.id,)
            ).fetchone():
                raise EventRejected(
                    f"bundle.claimed: claim id '{payload.id}' already exists."
                )
            member_claim_ids = [member.id for member in payload.member_claims]
            claim_placeholders = ",".join("?" for _ in member_claim_ids)
            collided_ids = [
                row[0]
                for row in conn.execute(
                    f"SELECT id FROM claims WHERE id IN ({claim_placeholders})",
                    tuple(member_claim_ids),
                ).fetchall()
            ]
            if collided_ids:
                raise EventRejected(
                    f"bundle.claimed: member claim ids already exist: {collided_ids}."
                )

            member_rows = conn.execute(
                "SELECT m.task_id, t.status, t.dependencies, t.likely_files, "
                "t.conflict_groups FROM execution_bundle_members m "
                "JOIN tasks t ON t.id = m.task_id WHERE m.bundle_id = ? "
                "ORDER BY m.position",
                (payload.bundle_id,),
            ).fetchall()
            member_ids = [member[0] for member in member_rows]
            supplied_ids = [member.task_id for member in payload.member_claims]
            if supplied_ids != member_ids:
                raise EventRejected(
                    "bundle.claimed: member claims must match stored member order."
                )
            not_ready = [row[0] for row in member_rows if row[1] != "ready"]
            if not_ready:
                raise EventRejected(
                    f"bundle.claimed: member tasks are not ready: {not_ready}."
                )
            graph = analyze_bundle_graph(
                member_ids,
                {member[0]: json.loads(member[2] or "[]") for member in member_rows},
            )
            if graph.dependency_cycle:
                raise EventRejected(
                    "bundle.claimed: member dependency cycle: "
                    + " -> ".join(graph.dependency_cycle)
                    + "."
                )
            throughput_budget = json.loads(row[3] or "{}")
            max_tasks = int(throughput_budget.get("max_tasks", 12))
            if len(member_ids) > max_tasks:
                raise EventRejected(
                    f"bundle.claimed: {len(member_ids)} members exceed "
                    f"max_tasks {max_tasks}."
                )
            max_serial_stages = int(throughput_budget.get("max_serial_stages", 6))
            if graph.critical_path_depth > max_serial_stages:
                raise EventRejected(
                    f"bundle.claimed: critical path {graph.critical_path_depth} "
                    f"exceeds max_serial_stages {max_serial_stages}."
                )

            members = set(member_ids)
            external_dependencies: list[str] = []
            expected_files: list[str] = []
            bundle_groups: set[str] = set()
            for member in member_rows:
                for dependency in json.loads(member[2] or "[]"):
                    if dependency not in members and dependency not in external_dependencies:
                        external_dependencies.append(dependency)
                for file_path in json.loads(member[3] or "[]"):
                    if file_path not in expected_files:
                        expected_files.append(file_path)
                bundle_groups.update(json.loads(member[4] or "[]"))
            if payload.expected_files != expected_files:
                raise EventRejected(
                    "bundle.claimed: expected_files must equal the ordered member union."
                )
            if external_dependencies:
                placeholders = ",".join("?" for _ in external_dependencies)
                dep_rows = conn.execute(
                    f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",
                    tuple(external_dependencies),
                ).fetchall()
                dep_status = {dep[0]: dep[1] for dep in dep_rows}
                blocked = [
                    dep for dep in external_dependencies if dep_status.get(dep) != "done"
                ]
                if blocked:
                    raise EventRejected(
                        f"bundle.claimed: external dependencies are not done: {blocked}."
                    )

            active_rows = conn.execute(
                "SELECT c.id, c.task_id, c.expected_files, t.conflict_groups "
                "FROM claims c JOIN tasks t ON t.id = c.task_id "
                "WHERE c.status = 'active' ORDER BY c.id"
            ).fetchall()
            conflicts: list[str] = []
            expected_set = set(expected_files)
            for active in active_rows:
                overlap = sorted(expected_set & set(json.loads(active[2] or "[]")))
                group_overlap = sorted(
                    bundle_groups & set(json.loads(active[3] or "[]"))
                )
                if active[1] in members or overlap or group_overlap:
                    conflicts.append(active[0])
            if conflicts:
                raise EventRejected(
                    f"bundle.claimed: conflicts with active claims: {conflicts}."
                )
        finally:
            conn.execute("COMMIT")

    def _write_bundle_claimed(
        self,
        conn: sqlite3.Connection,
        payload: BundleClaimedPayload,
        event: Event,
    ) -> None:
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            return
        event_time = event.timestamp.astimezone(datetime.UTC)
        if (
            event.actor != payload.claimed_by
            or payload.created_at != event_time
            or payload.last_heartbeat_at != event_time
            or payload.lease_expires_at <= event_time
        ):
            return
        bundle_row = conn.execute(
            "SELECT status, creation_event_id, throughput_budget, coordinator, "
            "created_at, updated_at "
            "FROM execution_bundles WHERE id = ?",
            (payload.bundle_id,),
        ).fetchone()
        if (
            bundle_row is None
            or bundle_row[0] != "planned"
            or bundle_row[1] != payload.creation_event_id
            or bundle_row[3] != payload.claimed_by
            or datetime.datetime.fromisoformat(bundle_row[4]) > event_time
            or datetime.datetime.fromisoformat(bundle_row[5]) > event_time
            or conn.execute(
                "SELECT 1 FROM bundle_claims WHERE bundle_id = ? AND status = 'active'",
                (payload.bundle_id,),
            ).fetchone()
        ):
            return
        member_ids = [member.task_id for member in payload.member_claims]
        placeholders = ",".join("?" for _ in member_ids)
        if conn.execute(
            "SELECT 1 FROM bundle_claims WHERE id = ?", (payload.id,)
        ).fetchone():
            return
        child_ids = [member.id for member in payload.member_claims]
        child_placeholders = ",".join("?" for _ in child_ids)
        if conn.execute(
            f"SELECT 1 FROM claims WHERE id IN ({child_placeholders}) LIMIT 1",
            tuple(child_ids),
        ).fetchone():
            return
        replay_members = conn.execute(
            "SELECT m.task_id, t.status, t.dependencies, t.likely_files, "
            "t.conflict_groups FROM execution_bundle_members m "
            "JOIN tasks t ON t.id = m.task_id WHERE m.bundle_id = ? "
            "ORDER BY m.position",
            (payload.bundle_id,),
        ).fetchall()
        if [row[0] for row in replay_members] != member_ids:
            return
        graph = analyze_bundle_graph(
            member_ids,
            {row[0]: json.loads(row[2] or "[]") for row in replay_members},
        )
        throughput_budget = json.loads(bundle_row[2] or "{}")
        if (
            len(member_ids) > int(throughput_budget.get("max_tasks", 12))
            or graph.dependency_cycle
            or graph.critical_path_depth
            > int(throughput_budget.get("max_serial_stages", 6))
        ):
            return
        expected_files: list[str] = []
        external_dependencies: list[str] = []
        bundle_groups: set[str] = set()
        member_set = set(member_ids)
        for replay_member in replay_members:
            for dependency in json.loads(replay_member[2] or "[]"):
                if dependency not in member_set and dependency not in external_dependencies:
                    external_dependencies.append(dependency)
            for path in json.loads(replay_member[3] or "[]"):
                if path not in expected_files:
                    expected_files.append(path)
            bundle_groups.update(json.loads(replay_member[4] or "[]"))
        if expected_files != payload.expected_files:
            return
        if external_dependencies:
            dep_placeholders = ",".join("?" for _ in external_dependencies)
            dep_rows = conn.execute(
                f"SELECT id, status FROM tasks WHERE id IN ({dep_placeholders})",
                tuple(external_dependencies),
            ).fetchall()
            dep_status = {row[0]: row[1] for row in dep_rows}
            if any(dep_status.get(dep) != "done" for dep in external_dependencies):
                return
        expected_set = set(expected_files)
        for active in conn.execute(
            "SELECT c.task_id, c.expected_files, t.conflict_groups "
            "FROM claims c JOIN tasks t ON t.id = c.task_id "
            "WHERE c.status = 'active' ORDER BY c.id"
        ).fetchall():
            if (
                active[0] in member_set
                or expected_set.intersection(json.loads(active[1] or "[]"))
                or bundle_groups.intersection(json.loads(active[2] or "[]"))
            ):
                return
        if conn.execute(
            f"SELECT 1 FROM tasks WHERE id IN ({placeholders}) "
            "AND status != 'ready' LIMIT 1",
            tuple(member_ids),
        ).fetchone() or conn.execute(
            f"SELECT 1 FROM claims WHERE task_id IN ({placeholders}) "
            "AND status = 'active' LIMIT 1",
            tuple(member_ids),
        ).fetchone():
            return
        member_claim_ids = {
            member.task_id: member.id for member in payload.member_claims
        }
        conn.execute(
            """INSERT INTO bundle_claims
               (id, bundle_id, claimed_by, status, branch, worktree_path,
                session_id, expected_files, member_claim_ids, created_at,
                lease_expires_at, last_heartbeat_at, released_at, release_reason)
               VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
            (
                payload.id,
                payload.bundle_id,
                payload.claimed_by,
                payload.branch,
                payload.worktree_path,
                payload.session_id,
                json.dumps(payload.expected_files),
                json.dumps(member_claim_ids, sort_keys=True),
                payload.created_at.isoformat(),
                payload.lease_expires_at.isoformat(),
                payload.last_heartbeat_at.isoformat(),
            ),
        )
        member_rows = conn.execute(
            "SELECT m.task_id, t.likely_files FROM execution_bundle_members m "
            "JOIN tasks t ON t.id = m.task_id WHERE m.bundle_id = ? "
            "ORDER BY m.position",
            (payload.bundle_id,),
        ).fetchall()
        claims_by_task = {member.task_id: member.id for member in payload.member_claims}
        for task_id, likely_files_json in member_rows:
            claim_id = claims_by_task[task_id]
            conn.execute(
                """INSERT INTO claims
                   (id, task_id, claimed_by, claim_type, status, branch,
                    worktree_path, session_id, bundle_claim_id, expected_files,
                    created_at, lease_expires_at, last_heartbeat_at,
                    released_at, release_reason)
                   VALUES (?, ?, ?, 'task', 'active', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
                (
                    claim_id,
                    task_id,
                    payload.claimed_by,
                    payload.branch,
                    payload.worktree_path,
                    payload.session_id,
                    payload.id,
                    likely_files_json or "[]",
                    payload.created_at.isoformat(),
                    payload.lease_expires_at.isoformat(),
                    payload.last_heartbeat_at.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO claim_replay_lineages "
                "(claim_id, creation_fingerprint) VALUES (?, ?)",
                (claim_id, f"bundle-child:{payload.id}:{task_id}"),
            )
            conn.execute(
                "UPDATE tasks SET status = 'claimed', updated_at = ? "
                "WHERE id = ? AND status = 'ready'",
                (event.timestamp.astimezone(datetime.UTC).isoformat(), task_id),
            )
        conn.execute(
            "UPDATE execution_bundles SET status = 'active', branch = ?, "
            "worktree_path = ?, updated_at = ? WHERE id = ? "
            "AND creation_event_id = ? AND status = 'planned'",
            (
                payload.branch,
                payload.worktree_path,
                event.timestamp.astimezone(datetime.UTC).isoformat(),
                payload.bundle_id,
                payload.creation_event_id,
            ),
        )

    @staticmethod
    def _check_bundle_progress_noted(
        conn: sqlite3.Connection,
        payload: BundleProgressNotedPayload,
        event: EventDraft,
    ) -> None:
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            raise EventRejected("bundle.progress_noted: event target mismatch.")
        if event.actor != payload.actor:
            raise EventRejected("bundle.progress_noted: event actor mismatch.")
        if payload.noted_at != event.timestamp.astimezone(datetime.UTC):
            raise EventRejected("bundle.progress_noted: noted_at must match event time.")
        row = conn.execute(
            "SELECT b.creation_event_id, b.status, c.id, c.claimed_by, c.status, "
            "c.lease_expires_at "
            "FROM execution_bundles b JOIN bundle_claims c ON c.bundle_id = b.id "
            "WHERE b.id = ?",
            (payload.bundle_id,),
        ).fetchone()
        if row is None:
            raise EventRejected("bundle.progress_noted: active claim not found.")
        if (
            row[0] != payload.creation_event_id
            or row[1] != "active"
            or row[2] != payload.bundle_claim_id
            or row[3] != payload.actor
            or row[4] != "active"
        ):
            raise EventRejected("bundle.progress_noted: coordinator claim mismatch.")
        if datetime.datetime.fromisoformat(row[5]) < event.timestamp.astimezone(
            datetime.UTC
        ):
            raise EventRejected("bundle.progress_noted: coordinator lease has expired.")
        members = {
            member[0]
            for member in conn.execute(
                "SELECT task_id FROM execution_bundle_members WHERE bundle_id = ?",
                (payload.bundle_id,),
            ).fetchall()
        }
        outside = [task for task in payload.member_task_ids if task not in members]
        if outside:
            raise EventRejected(
                f"bundle.progress_noted: non-member tasks referenced: {outside}."
            )

    @staticmethod
    def _bundle_claim_lifecycle_row(
        conn: sqlite3.Connection, bundle_claim_id: str, bundle_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT claimed_by, status, lease_expires_at, last_heartbeat_at "
            "FROM bundle_claims WHERE id = ? AND bundle_id = ?",
            (bundle_claim_id, bundle_id),
        ).fetchone()

    def _check_bundle_claim_renewed(
        self,
        conn: sqlite3.Connection,
        payload: BundleClaimRenewedPayload,
        event: EventDraft,
    ) -> None:
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            raise EventRejected("bundle.claim_renewed: event target mismatch.")
        if event.actor != payload.renewed_by:
            raise EventRejected("bundle.claim_renewed: event actor mismatch.")
        row = self._bundle_claim_lifecycle_row(
            conn, payload.bundle_claim_id, payload.bundle_id
        )
        if row is None or row[1] != "active":
            raise EventRejected("bundle.claim_renewed: active claim not found.")
        if row[0] != payload.renewed_by:
            raise EventRejected("bundle.claim_renewed: only the coordinator may renew.")
        event_time = event.timestamp.astimezone(datetime.UTC)
        if payload.last_heartbeat_at != event_time:
            raise EventRejected("bundle.claim_renewed: heartbeat must match event time.")
        if datetime.datetime.fromisoformat(row[2]) < event_time:
            raise EventRejected("bundle.claim_renewed: claim lease has expired.")
        if datetime.datetime.fromisoformat(row[3]) >= event_time:
            raise EventRejected("bundle.claim_renewed: heartbeat must move forward.")
        if payload.lease_expires_at <= datetime.datetime.fromisoformat(row[2]):
            raise EventRejected("bundle.claim_renewed: new expiry must extend the lease.")
        if payload.lease_expires_at <= payload.last_heartbeat_at:
            raise EventRejected("bundle.claim_renewed: expiry must follow heartbeat.")

    @staticmethod
    def _write_bundle_claim_renewed(
        conn: sqlite3.Connection,
        payload: BundleClaimRenewedPayload,
        event: Event,
    ) -> None:
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            return
        row = conn.execute(
            "SELECT claimed_by, lease_expires_at, last_heartbeat_at FROM bundle_claims "
            "WHERE id = ? AND bundle_id = ? AND status = 'active'",
            (payload.bundle_claim_id, payload.bundle_id),
        ).fetchone()
        event_time = event.timestamp.astimezone(datetime.UTC)
        if (
            row is None
            or event.actor != payload.renewed_by
            or row[0] != payload.renewed_by
            or payload.last_heartbeat_at != event_time
            or datetime.datetime.fromisoformat(row[1]) < event_time
            or datetime.datetime.fromisoformat(row[2]) >= event_time
            or payload.lease_expires_at <= event_time
            or payload.lease_expires_at <= datetime.datetime.fromisoformat(row[1])
        ):
            return
        cursor = conn.execute(
            "UPDATE bundle_claims SET lease_expires_at = ?, last_heartbeat_at = ? "
            "WHERE id = ? AND bundle_id = ? AND status = 'active'",
            (
                payload.lease_expires_at.isoformat(),
                payload.last_heartbeat_at.isoformat(),
                payload.bundle_claim_id,
                payload.bundle_id,
            ),
        )
        if cursor.rowcount != 1:
            return
        conn.execute(
            "UPDATE claims SET lease_expires_at = ?, last_heartbeat_at = ? "
            "WHERE bundle_claim_id = ? AND status = 'active'",
            (
                payload.lease_expires_at.isoformat(),
                payload.last_heartbeat_at.isoformat(),
                payload.bundle_claim_id,
            ),
        )

    def _check_bundle_claim_released(
        self,
        conn: sqlite3.Connection,
        payload: BundleClaimReleasedPayload,
        event: EventDraft,
    ) -> None:
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            raise EventRejected("bundle.claim_released: event target mismatch.")
        if event.actor != payload.released_by:
            raise EventRejected("bundle.claim_released: event actor mismatch.")
        row = conn.execute(
            "SELECT c.claimed_by, c.status, c.created_at, c.last_heartbeat_at, "
            "b.updated_at FROM bundle_claims c "
            "JOIN execution_bundles b ON b.id = c.bundle_id "
            "WHERE c.id = ? AND c.bundle_id = ?",
            (payload.bundle_claim_id, payload.bundle_id),
        ).fetchone()
        if row is None:
            raise EventRejected("bundle.claim_released: active claim not found.")
        if not payload.force and row[0] != payload.released_by:
            raise EventRejected("bundle.claim_released: only the coordinator may release.")
        if row[1] != "active":
            raise IdempotentNoOp("bundle coordinator claim is already terminal")
        event_time = event.timestamp.astimezone(datetime.UTC)
        if max(
            datetime.datetime.fromisoformat(row[2]),
            datetime.datetime.fromisoformat(row[3]),
            datetime.datetime.fromisoformat(row[4]),
        ) > event_time:
            raise EventRejected("bundle.claim_released: release predates claim state.")

    @staticmethod
    def _write_bundle_claim_released(
        conn: sqlite3.Connection,
        payload: BundleClaimReleasedPayload,
        event: Event,
    ) -> None:
        row = conn.execute(
            "SELECT c.claimed_by, c.created_at, c.last_heartbeat_at, b.updated_at "
            "FROM bundle_claims c JOIN execution_bundles b ON b.id = c.bundle_id "
            "WHERE c.id = ? AND c.bundle_id = ? AND c.status = 'active'",
            (payload.bundle_claim_id, payload.bundle_id),
        ).fetchone()
        event_time = event.timestamp.astimezone(datetime.UTC)
        if (
            event.target_kind != "bundle"
            or event.target_id != payload.bundle_id
            or event.actor != payload.released_by
            or row is None
            or (not payload.force and row[0] != payload.released_by)
            or max(
                datetime.datetime.fromisoformat(row[1]),
                datetime.datetime.fromisoformat(row[2]),
                datetime.datetime.fromisoformat(row[3]),
            )
            > event_time
        ):
            return
        SqliteBackend._write_bundle_claim_terminal(
            conn,
            bundle_claim_id=payload.bundle_claim_id,
            bundle_id=payload.bundle_id,
            terminal_status="force_released" if payload.force else "released",
            timestamp=event_time.isoformat(),
            reason=payload.release_reason,
        )

    def _check_bundle_claim_stale(
        self,
        conn: sqlite3.Connection,
        payload: BundleClaimStalePayload,
        event: EventDraft,
    ) -> None:
        if event.target_kind != "bundle" or event.target_id != payload.bundle_id:
            raise EventRejected("bundle.claim_stale: event target mismatch.")
        if event.actor != payload.actor:
            raise EventRejected("bundle.claim_stale: event actor mismatch.")
        if payload.detected_at != event.timestamp.astimezone(datetime.UTC):
            raise EventRejected("bundle.claim_stale: detected_at must match event time.")
        row = self._bundle_claim_lifecycle_row(
            conn, payload.bundle_claim_id, payload.bundle_id
        )
        if row is None or row[1] != "active":
            raise EventRejected("bundle.claim_stale: active claim not found.")
        if datetime.datetime.fromisoformat(row[2]) >= payload.detected_at:
            raise EventRejected("bundle.claim_stale: lease has not expired.")

    @staticmethod
    def _write_bundle_claim_stale(
        conn: sqlite3.Connection,
        payload: BundleClaimStalePayload,
        event: Event,
    ) -> None:
        row = conn.execute(
            "SELECT lease_expires_at FROM bundle_claims WHERE id = ? "
            "AND bundle_id = ? AND status = 'active'",
            (payload.bundle_claim_id, payload.bundle_id),
        ).fetchone()
        event_time = event.timestamp.astimezone(datetime.UTC)
        if (
            event.target_kind != "bundle"
            or event.target_id != payload.bundle_id
            or event.actor != payload.actor
            or payload.detected_at != event_time
            or row is None
            or datetime.datetime.fromisoformat(row[0]) >= event_time
        ):
            return
        SqliteBackend._write_bundle_claim_terminal(
            conn,
            bundle_claim_id=payload.bundle_claim_id,
            bundle_id=payload.bundle_id,
            terminal_status="stale",
            timestamp=event_time.isoformat(),
            reason="lease_expired",
        )

    @staticmethod
    def _write_bundle_claim_terminal(
        conn: sqlite3.Connection,
        *,
        bundle_claim_id: str,
        bundle_id: str,
        terminal_status: str,
        timestamp: str,
        reason: str | None,
    ) -> None:
        active_tasks = [
            row[0]
            for row in conn.execute(
                "SELECT task_id FROM claims WHERE bundle_claim_id = ? "
                "AND status = 'active' ORDER BY task_id",
                (bundle_claim_id,),
            ).fetchall()
        ]
        conn.execute(
            "UPDATE bundle_claims SET status = ?, released_at = ?, "
            "release_reason = ? WHERE id = ? AND bundle_id = ? AND status = 'active'",
            (terminal_status, timestamp, reason, bundle_claim_id, bundle_id),
        )
        conn.execute(
            "UPDATE claims SET status = ?, released_at = ?, release_reason = ? "
            "WHERE bundle_claim_id = ? AND status = 'active'",
            (terminal_status, timestamp, reason, bundle_claim_id),
        )
        for task_id in active_tasks:
            conn.execute(
                "UPDATE tasks SET status = 'ready', updated_at = ? WHERE id = ? "
                "AND status IN ('claimed', 'in_progress', 'blocked')",
                (timestamp, task_id),
            )
        conn.execute(
            "UPDATE execution_bundles SET status = 'replan_required', updated_at = ? "
            "WHERE id = ? AND status = 'active'",
            (timestamp, bundle_id),
        )

    @staticmethod
    def _normalize_task_payload(task_dict: dict[str, Any]) -> dict[str, Any]:
        """Coerce a minimal Task payload's None scores/verification to ``{}``.

        Task.scores / Task.verification are required submodels; the payload
        allows None so MCP / hand-rolled callers can send a minimal task without
        preloading sentinels. Pure dict munging — shared by the check and write
        phases so both validate / build the identical normalized shape.
        """
        task_dict = dict(task_dict)
        if task_dict.get("scores") is None:
            task_dict["scores"] = {}
        if task_dict.get("verification") is None:
            task_dict["verification"] = {}
        return task_dict

    def _check_task_created(
        self,
        conn: sqlite3.Connection,
        payload: TaskCreatedPayload,
        event: EventDraft,
    ) -> None:
        """Validate the (normalized) Task payload before any write.

        Was a validation guard inside the old handler (``raise
        TransactionAborted`` on an invalid Task); now rejects up front.
        """
        _ = (conn, event)
        task_dict = self._normalize_task_payload(payload.model_dump(mode="json"))
        try:
            Task.model_validate(task_dict)
        except Exception as exc:
            raise EventRejected(
                f"task.created: invalid Task payload: {exc}"
            ) from exc

    def _write_task_created(
        self,
        conn: sqlite3.Connection,
        payload: TaskCreatedPayload,
        event: Event,
    ) -> None:
        """Insert a Task row from the event payload.

        Payload fields: all Task model fields.  Scores may be None for all
        dimensions at creation time; they get populated by task.scored later.

        The payload was already validated by ``_check_task_created``; the
        ``model_validate`` here is an infallible rebuild.
        """
        task_dict = self._normalize_task_payload(payload.model_dump(mode="json"))
        task = Task.model_validate(task_dict)
        self._insert_task_row(conn, task)

    @staticmethod
    def _build_task_score(payload: TaskScoredPayload) -> Score:
        """Build the Score model from a task.scored payload (pure)."""
        score_data = dict(payload.scores)
        score_data["explanation"] = payload.explanation
        return Score.model_validate(score_data)

    def _check_task_scored(
        self,
        conn: sqlite3.Connection,
        payload: TaskScoredPayload,
        event: EventDraft,
    ) -> None:
        """Validate the scores payload and confirm the task exists.

        Was two validation guards in the old handler (invalid scores payload;
        ``task not found`` after a 0-row UPDATE); both now reject up front.
        """
        _ = event
        try:
            self._build_task_score(payload)
        except Exception as exc:
            raise EventRejected(
                f"task.scored: invalid scores payload: {exc}"
            ) from exc
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (payload.task_id,)
        ).fetchone()
        if row is None:
            raise EventRejected(f"task.scored: task '{payload.task_id}' not found.")

    def _write_task_scored(
        self,
        conn: sqlite3.Connection,
        payload: TaskScoredPayload,
        event: Event,
    ) -> None:
        """Update a task's scores and explanation.

        Payload fields:
            task_id (str) — required
            scores (dict[str, int | None]) — dimension name → score; required
            explanation (str) — required

        The scores dict is merged with any existing Score fields; null-valued
        dimensions remain null (not coerced to 0).  The explanation is stored
        inside the Score model's ``explanation`` field.

        ``_check_task_scored`` already proved the scores validate and the task
        exists, so this UPDATE always hits a row.
        """
        task_id: str = payload.task_id
        timestamp: str = event.timestamp.isoformat()
        # Merge the incoming scores OVER the existing Score, per this method's
        # contract (the code previously overwrote wholesale, contradicting the
        # docstring above). Fields absent from the payload are PRESERVED, not
        # reset to model defaults — critically the risk-confirmation flags the
        # review gate sets: an ordinary re-score carries only the six numeric
        # dimensions, and a wholesale overwrite would silently clear
        # blast_radius_confirmed / review_risk_confirmed and evict a confirmed
        # within-ceiling task from a ceilinged runner's queue with no path back.
        row = conn.execute(
            "SELECT scores FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        existing = json.loads(row[0]) if row and row[0] else {}
        merged = {**existing, **dict(payload.scores)}
        merged["explanation"] = payload.explanation
        score = Score.model_validate(merged)
        scores_json = json.dumps(score.model_dump(mode="json"))

        conn.execute(
            """
            UPDATE tasks
               SET scores = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (scores_json, timestamp, task_id),
        )

    def _normalize_subtask(
        self, subtask_data: Any, parent_task_id: str
    ) -> dict[str, Any]:
        """Force the parent id and coerce minimal scores/verification (pure)."""
        normalized: dict[str, Any] = self._normalize_task_payload(dict(subtask_data))
        normalized["parent_task_id"] = parent_task_id
        return normalized

    def _check_task_expanded(
        self,
        conn: sqlite3.Connection,
        payload: TaskExpandedPayload,
        event: EventDraft,
    ) -> None:
        """Reject an empty expansion and validate every subtask payload.

        Was two validation guards in the old handler (empty ``subtasks`` list;
        invalid subtask payload); both now reject up front.
        """
        _ = (conn, event)
        if not payload.subtasks:
            raise EventRejected(
                "task.expanded payload has empty 'subtasks' list; nothing to expand."
            )
        for subtask_data in payload.subtasks:
            normalized = self._normalize_subtask(subtask_data, payload.parent_task_id)
            try:
                Task.model_validate(normalized)
            except Exception as exc:
                raise EventRejected(
                    f"task.expanded: invalid subtask payload: {exc}"
                ) from exc

    def _write_task_expanded(
        self,
        conn: sqlite3.Connection,
        payload: TaskExpandedPayload,
        event: Event,
    ) -> None:
        """Insert subtask rows derived from expanding a parent task.

        Payload fields:
            parent_task_id (str) — required; must exist in tasks table
            subtasks (list[dict]) — list of Task payloads; each will be
                                    inserted with parent_task_id set

        The parent task's status is NOT changed here; the subtask rows
        themselves signal expansion (parent_task_id IS NOT NULL).

        ``_check_task_expanded`` already proved the list is non-empty and every
        subtask validates, so each ``model_validate`` here is an infallible
        rebuild.
        """
        _ = event
        parent_task_id: str = payload.parent_task_id
        for subtask_data in payload.subtasks:
            normalized = self._normalize_subtask(subtask_data, parent_task_id)
            subtask = Task.model_validate(normalized)
            self._insert_task_row(conn, subtask)

    def _check_task_status_changed(
        self,
        conn: sqlite3.Connection,
        payload: TaskStatusChangedPayload,
        event: EventDraft,
    ) -> None:
        """Decide the transition outcome before any write.

        The old handler ran the guarded UPDATE then interpreted a 0-row result:
        task-not-found / concurrency-drift were ``TransactionAborted``;
        already-at-target was a silent ``return``. This check reproduces those
        decisions on read-only state — reject (not found / drift) or signal a
        silent ``IdempotentNoOp`` (already at target).
        """
        _ = event
        task_id: str = payload.task_id
        from_status: str = payload.from_status
        to_status: str = payload.to_status

        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise EventRejected(f"task.status_changed: task '{task_id}' not found.")
        actual_status = row[0]
        if actual_status == from_status:
            return  # proceed — the guarded UPDATE will match.
        # Idempotent re-application: already at the target status. This lets
        # `plan` (which emits proposed→drafted) be re-run safely after the
        # first run promoted tasks. The old handler returned silently here — no
        # warn log — so this IdempotentNoOp carries no warn metadata.
        if actual_status == to_status:
            raise _idempotent_no_op(
                f"task.status_changed: task '{task_id}' already at '{to_status}'."
            )
        raise EventRejected(
            f"task.status_changed: concurrency guard failed for task '{task_id}'. "
            f"Expected status '{from_status}', got '{actual_status}'. "
            "The task status may have been changed by a concurrent operation."
        )

    def _write_task_status_changed(
        self,
        conn: sqlite3.Connection,
        payload: TaskStatusChangedPayload,
        event: Event,
    ) -> None:
        """Atomically transition a task from one status to another.

        Payload fields:
            task_id (str) — required
            from (str)    — expected current status (concurrency guard)
            to (str)      — target status
            reason (str | None) — optional human-readable reason

        ``_check_task_status_changed`` already proved the task exists and is at
        ``from_status``; the WHERE-status guard remains as a defensive belt but
        always matches here.
        """
        timestamp: str = event.timestamp.isoformat()
        conn.execute(
            """
            UPDATE tasks
               SET status = ?,
                   updated_at = ?
             WHERE id = ?
               AND status = ?
            """,
            (payload.to_status, timestamp, payload.task_id, payload.from_status),
        )

    # ------------------------------------------------------------------
    # v1.15.0 handlers — orphan cleanup on PRD re-parse
    # ------------------------------------------------------------------

    # Task statuses that may be deleted without an explicit `force=True`.
    # Anything outside this set carries claim/evidence history and would
    # silently lose audit data on delete. The handler refuses those unless
    # the caller (via `anvil plan --prune-force`) explicitly accepts
    # the risk.
    _DELETABLE_TASK_STATUSES: frozenset[str] = frozenset({
        "proposed", "drafted", "ready",
    })

    def _check_task_deleted(
        self,
        conn: sqlite3.Connection,
        payload: TaskDeletedPayload,
        event: EventDraft,
    ) -> None:
        """Refuse deletion of a missing / unsafe / FK-protected task.

        Was three validation guards in the old handler:
        1. Status check — refuses unless safe-deletable or ``force=True``.
        2. Existence check — task must exist.
        3. Audit-FK check — ``claims`` and ``evidence`` are RESTRICT-FK'd on
           ``task_id``; a referenced task cannot be deleted (``force`` does NOT
           bypass this — those tables hold the protected audit history).

        All three now reject up front, before the cleanup write runs.
        """
        _ = event
        task_id = payload.task_id

        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise EventRejected(
                f"task.deleted: task '{task_id}' not found in state.db"
            )
        current_status: str = row[0]

        if not payload.force and current_status not in self._DELETABLE_TASK_STATUSES:
            raise EventRejected(
                f"task.deleted: refusing to delete task '{task_id}' in status "
                f"'{current_status}' without force=True. "
                f"Safe-delete statuses: {sorted(self._DELETABLE_TASK_STATUSES)}. "
                "Release any active claim, complete the work, or pass "
                "force=True (via `anvil plan --prune-force`) to "
                "delete despite the status."
            )

        claim_count = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        evidence_count = conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        bundle_count = conn.execute(
            "SELECT COUNT(*) FROM execution_bundle_members WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        if claim_count or evidence_count or bundle_count:
            raise EventRejected(
                f"task.deleted: cannot delete task '{task_id}' — it has "
                f"{claim_count} claim row(s) and {evidence_count} evidence "
                f"row(s), plus {bundle_count} bundle membership row(s), that "
                "are FK-protected by schema. The audit history "
                "intentionally outlives the task. Re-add the task to "
                "prd.md if you want to preserve a working entry, or "
                "accept that the orphan is conceptually dropped but its "
                "row stays (the data is reachable via events.jsonl)."
            )

    def _write_task_deleted(
        self,
        conn: sqlite3.Connection,
        payload: TaskDeletedPayload,
        event: Event,  # noqa: ARG002 — event metadata recorded via JSONL only
    ) -> None:
        """Delete a Task row after ``_check_task_deleted`` cleared it.

        Cleanup walk (the precondition guards live in ``_check_task_deleted``):
        3. ``conflict_groups.task_ids`` — JSON array; rewrite to drop the
           deleted task ID. Cosmetic since the row is going anyway, but
           keeps the groups table self-consistent.
        4. ``tasks`` — the row itself. ``parent_task_id ON DELETE SET
           NULL`` automatically detaches any child subtasks (they
           become orphaned rather than cascade-deleted, by design).

        Audit history preserved: ``events`` rows targeting this task ID
        stay in events.jsonl forever. ``events.target_id = 'T014'`` will
        still resolve to the now-gone task on replay — that is the
        intended audit-trail behaviour and the reason this handler does
        not touch the events table.
        """
        task_id = payload.task_id

        # 3. Rewrite conflict_groups.task_ids JSON arrays to remove this task.
        # Explicit Row indexing (row["id"]) is safer than positional unpack —
        # the connection's row_factory = sqlite3.Row makes this self-documenting
        # and survives column-order changes. Critic SHOULD FIX from PR #63.
        groups = conn.execute(
            "SELECT id, task_ids FROM conflict_groups"
        ).fetchall()
        for row in groups:
            group_id = row["id"]
            task_ids_json = row["task_ids"]
            try:
                task_ids = json.loads(task_ids_json) if task_ids_json else []
            except (TypeError, ValueError):
                # Malformed JSON in a conflict_group row would otherwise be
                # silently left alone — meaning a subsequent query reading
                # that group could still see the deleted task ID. Log the
                # corruption to stderr so the operator sees it AND rewrite
                # the row to an empty array so downstream queries are
                # consistent with what was actually deleted.
                print(
                    f"warning: conflict_groups row {group_id!r} has malformed "
                    f"task_ids JSON ({task_ids_json!r}); resetting to "
                    "empty array as part of task.deleted cleanup.",
                    file=sys.stderr,
                )
                conn.execute(
                    "UPDATE conflict_groups SET task_ids = ? WHERE id = ?",
                    ("[]", group_id),
                )
                continue
            if task_id in task_ids:
                task_ids = [t for t in task_ids if t != task_id]
                conn.execute(
                    "UPDATE conflict_groups SET task_ids = ? WHERE id = ?",
                    (json.dumps(task_ids), group_id),
                )

        # 4. The task row itself.
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def _check_feature_deleted(
        self,
        conn: sqlite3.Connection,
        payload: FeatureDeletedPayload,
        event: EventDraft,
    ) -> None:
        """Refuse deletion of a missing or still-referenced feature.

        Was two validation guards in the old handler (feature not found;
        referencing tasks still present). The schema has ``tasks.feature_id
        REFERENCES features(id) ON DELETE RESTRICT``, so the DELETE would
        already fail with a generic FK error; pre-checking names the actual
        blocking task IDs. ``force=True`` does NOT bypass this — deleting a
        feature with tasks is data corruption, not an acceptable risk.
        """
        _ = event
        feature_id = payload.feature_id

        row = conn.execute(
            "SELECT id FROM features WHERE id = ?", (feature_id,)
        ).fetchone()
        if row is None:
            raise EventRejected(
                f"feature.deleted: feature '{feature_id}' not found in state.db"
            )

        blocking = conn.execute(
            "SELECT id FROM tasks WHERE feature_id = ? ORDER BY id", (feature_id,)
        ).fetchall()
        if blocking:
            blocking_ids = [r[0] for r in blocking]
            raise EventRejected(
                f"feature.deleted: refusing to delete feature '{feature_id}' "
                f"while tasks still reference it: {blocking_ids}. "
                "Delete those tasks first (the orphan-prune flow in `plan` "
                "does this in the right order — tasks before features)."
            )

    def _write_feature_deleted(
        self,
        conn: sqlite3.Connection,
        payload: FeatureDeletedPayload,
        event: Event,  # noqa: ARG002 — event metadata recorded via JSONL only
    ) -> None:
        """Delete the Feature row after ``_check_feature_deleted`` cleared it."""
        conn.execute("DELETE FROM features WHERE id = ?", (payload.feature_id,))

    # ------------------------------------------------------------------
    # Phase 8 handler — pull-applies-remote (P1-1 fix)
    # ------------------------------------------------------------------

    def _check_task_synced_from_remote(
        self,
        conn: sqlite3.Connection,
        payload: TaskSyncedFromRemotePayload,
        event: EventDraft,
    ) -> None:
        """Confirm the target task exists before the remote overwrite.

        Was the ``task not found`` guard (0-row UPDATE) in the old handler; now
        rejects up front. No from-status concurrency guard — the sync pull path
        already proved local was untouched before emitting this event.
        """
        _ = event
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (payload.task_id,)
        ).fetchone()
        if row is None:
            raise EventRejected(
                f"task.synced_from_remote: task '{payload.task_id}' not found."
            )

    def _write_task_synced_from_remote(
        self,
        conn: sqlite3.Connection,
        payload: TaskSyncedFromRemotePayload,
        event: Event,
    ) -> None:
        """Overwrite a Task's title / description / status from a remote pull.

        Emitted by the sync CLI's pull path on the
        ``remote_moved and not local_moved`` branch — i.e. the remote
        legitimately moved ahead and there is no local divergence to
        protect. The handler rewrites exactly the three fields the
        forbid-extras payload model exposes (so no Task field outside
        the remote's known shape can be silently lost) and bumps
        ``updated_at`` to the event timestamp.

        Does NOT touch the ``sync_mappings`` row — the caller emits a
        separate ``sync_mapping.upserted`` event for that, keeping the
        mutation surfaces orthogonal (a future "rebuild local from
        remote" replay should NOT also bump the mapping's
        last_synced_at; that's a separate decision).

        ``status`` follows the same field-set rules as
        ``task.status_changed`` — the value must be a valid TaskStatus
        string. We do NOT enforce a from-status concurrency guard here:
        the sync pull path already proved local was untouched
        (``local_moved == False``) before emitting this event, so the
        guard would just duplicate work the caller already did.
        """
        task_id: str = payload.task_id
        title: str = payload.title
        description: str = payload.description
        status: str = payload.status
        timestamp: str = event.timestamp.isoformat()

        conn.execute(
            """
            UPDATE tasks
               SET title = ?,
                   description = ?,
                   status = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (title, description, status, timestamp, task_id),
        )

    # ------------------------------------------------------------------
    # Phase 4 handlers — claim lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _claim_creation_fingerprint(payload: ClaimCreatedPayload) -> str:
        """Return a canonical identity for one immutable claim creation fact."""
        return json.dumps(
            payload.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _claim_replay_collision(conn: sqlite3.Connection, claim_id: str) -> bool:
        row = conn.execute(
            "SELECT collision_detected FROM claim_replay_lineages "
            "WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()
        return row is not None and bool(row[0])

    @classmethod
    def _reject_claim_replay_collision(
        cls, conn: sqlite3.Connection, claim_id: str, action: str
    ) -> None:
        if cls._claim_replay_collision(conn, claim_id):
            raise EventRejected(
                f"{action}: claim '{claim_id}' has divergent creation lineages; "
                "descendant events are quarantined."
            )

    def _check_claim_created(
        self,
        conn: sqlite3.Connection,
        payload: ClaimCreatedPayload,
        event: EventDraft,
    ) -> None:
        """Decide whether a claim may transition its task ready → claimed.

        The old handler ran the INSERT OR IGNORE + guarded UPDATE, then on a
        0-row UPDATE: ``task not found`` → TransactionAborted; already
        ``'claimed'`` → silent return (replay of a committed claim); any other
        status → concurrency TransactionAborted. This check reproduces the
        reject decisions on read-only state.

        Note: the already-``'claimed'`` case is **not** an ``IdempotentNoOp``
        here — the write must still run so the INSERT OR IGNORE happens. With a
        ready/claimed task the UPDATE simply matches 0 rows, exactly as before,
        and the claim INSERT OR IGNORE is idempotent on its PK. Treating it as a
        no-op would skip the write and change behavior for a fresh claim PK
        against an already-claimed task.

        TOCTOU fix (in-transaction overlap re-check): the pre-claim file-overlap
        check in ``ClaimManager.check_conflicts`` runs OUTSIDE this serialized
        write path, so two concurrent claims on *different* task rows whose
        ``expected_files`` overlap can both pass it and both reach append().
        This check is the source of truth: it re-validates ``expected_files``
        against every OTHER active claim while holding the same flock +
        threading lock that serializes appends. The first claim to enter the
        critical section commits its row; the second sees it here and is
        rejected — unless this claim was made with ``--force`` (threaded through
        ``payload.force``), which overrides the rejection exactly like it
        overrides the pre-check. Replayed events carry ``force`` absent →
        defaults to False, but replay applies via ``_write_*`` only and never
        runs this check, so the default is irrelevant on the replay path.
        """
        if event.target_kind != "claim" or event.target_id != payload.id:
            raise EventRejected(
                "claim.created: event target must be claim "
                f"'{payload.id}', got {event.target_kind} '{event.target_id}'."
            )
        # WAL snapshot freshness (TOCTOU correctness). This check runs in the
        # append() validation phase, and the connection is in autocommit mode.
        # Earlier reads in THIS same claim command (get_task / get_prd /
        # check_conflicts / list_active_claims and the stale-claim reaper) leave
        # the connection pinned to the WAL read snapshot they opened — and a
        # bare autocommit SELECT here can keep reading THAT stale snapshot, which
        # predates a competing claim's COMMIT. Under the flock our append is
        # strictly serialized AFTER any competing claim's COMMIT (verified by
        # monotonic event ids), so the committed row exists on disk; the only
        # thing hiding it is the stale read mark. Acquiring the SQLite write lock
        # with BEGIN IMMEDIATE forces a brand-new snapshot that includes every
        # prior commit (no other writer can be mid-transaction — we hold both the
        # flock and now the write lock), so the concurrency reads below see the
        # winner's claim row. We COMMIT (read-only, releases the lock) before
        # returning so append()'s Phase-5 BEGIN IMMEDIATE can run. A plain
        # rollback() is NOT enough: in autocommit it is a no-op and does not
        # refresh the read mark.
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_claim_created_locked(conn, payload)
        finally:
            # Release the write lock we took purely to refresh the snapshot.
            # COMMIT not ROLLBACK: we wrote nothing, and COMMIT is the cheaper
            # path to drop the lock without an abort log line.
            conn.execute("COMMIT")

    def _validate_claim_created_locked(
        self,
        conn: sqlite3.Connection,
        payload: ClaimCreatedPayload,
    ) -> None:
        """Run the claim.created concurrency guards against a fresh snapshot.

        Invoked from ``_check_claim_created`` while holding a BEGIN IMMEDIATE
        write lock, so every read sees the latest committed state. Raises
        :class:`EventRejected` on any guard failure; performs no writes.
        """
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (payload.task_id,)
        ).fetchone()
        if row is None:
            raise EventRejected(f"claim.created: task '{payload.task_id}' not found.")
        actual_status = row[0]
        if actual_status not in ("ready", "claimed"):
            raise EventRejected(
                f"claim.created: concurrency guard failed for task "
                f"'{payload.task_id}'. Expected status 'ready', got "
                f"'{actual_status}'. Another claim may have already acquired "
                "this task."
            )

        terminal = tuple(status.value for status in TERMINAL_BUNDLE_STATUSES)
        placeholders = ",".join("?" for _ in terminal)
        active_bundle = conn.execute(
            f"""SELECT b.id
                  FROM execution_bundle_members m
                  JOIN execution_bundles b ON b.id = m.bundle_id
                 WHERE m.task_id = ?
                   AND b.status NOT IN ({placeholders})
                 ORDER BY b.id
                 LIMIT 1""",
            (payload.task_id,) + terminal,
        ).fetchone()
        if active_bundle is not None:
            raise EventRejected(
                f"claim.created: task '{payload.task_id}' belongs to active "
                f"execution bundle '{active_bundle[0]}'. Use the bundle "
                "coordinator claim flow instead of an independent task claim."
            )

        # Replay / idempotent re-apply: if THIS claim id is already present we
        # are re-seeing a committed event (crash-recovery forward catch-up or a
        # duplicated git-merged line). Its own row would otherwise register as a
        # self-conflict; skip the guards entirely — INSERT OR IGNORE makes the
        # write a no-op anyway.
        existing = conn.execute(
            "SELECT 1 FROM claims WHERE id = ?", (payload.id,)
        ).fetchone()
        if existing is not None:
            return

        # Same-task race guard (TOCTOU): the status check above admits a
        # 'claimed' task so a replayed claim.created can still run its
        # idempotent write. That tolerance is exactly what lets a *concurrent*
        # second claim slip through — the loser of a same-task race finds the
        # task already 'claimed', passes the status check, and (pre-fix) inserts
        # a SECOND active claim row whose 0-row task UPDATE goes unnoticed.
        # Reject whenever a DIFFERENT active claim already exists on this task.
        # This is never overridable by --force: two live leases on one task is
        # never valid (force only overrides cross-task file/group overlap).
        active_same_task = conn.execute(
            "SELECT id, claimed_by FROM claims "
            "WHERE task_id = ? AND status = 'active' AND id != ?",
            (payload.task_id, payload.id),
        ).fetchone()
        if active_same_task is not None:
            raise EventRejected(
                f"claim.created: concurrency guard failed for task "
                f"'{payload.task_id}'. Active claim '{active_same_task[0]}' by "
                f"'{active_same_task[1]}' already holds this task. Another claim "
                "acquired it first."
            )

        self._check_claim_file_overlap(conn, payload)
        self._check_claim_group_overlap(conn, payload)

    def _check_claim_file_overlap(
        self,
        conn: sqlite3.Connection,
        payload: ClaimCreatedPayload,
    ) -> None:
        """Reject the claim if its ``expected_files`` overlap an active claim.

        Runs inside ``append()``'s serialized critical section (flock + thread
        lock), making it the atomic source of truth for file-overlap conflicts
        — the partner half of the TOCTOU fix described in
        ``_check_claim_created``. A conflict is an active claim by a DIFFERENT
        actor on a DIFFERENT task that shares at least one ``expected_files``
        entry. Same-actor and same-task active claims are not conflicts (they
        mirror ``ClaimManager.check_conflicts``' self/own-task skips). With
        ``payload.force`` set, the overlap is logged and allowed through.
        """
        new_files: set[str] = {str(f) for f in (payload.expected_files or [])}
        if not new_files:
            return

        rows = conn.execute(
            "SELECT id, claimed_by, task_id, expected_files, bundle_claim_id "
            "FROM claims WHERE status = 'active'"
        ).fetchall()

        for row in rows:
            other_id = row[0]
            other_actor = row[1]
            other_task_id = row[2]
            # Skip the owning actor's own claims and same-task re-claims, exactly
            # like the pre-claim check in ClaimManager.check_conflicts.
            if other_task_id == payload.task_id:
                continue
            other_bundle_claim_id = row[4]
            if other_actor == payload.claimed_by and other_bundle_claim_id is None:
                continue
            try:
                other_files = set(json.loads(row[3] or "[]"))
            except (json.JSONDecodeError, TypeError):
                continue
            overlap = sorted(new_files & {str(f) for f in other_files})
            if not overlap:
                continue
            if payload.force and other_bundle_claim_id is None:
                logger.warning(
                    "Forced claim %r on task %r: in-transaction file overlap "
                    "with active claim %r by %r (files: %r) — overridden.",
                    payload.id,
                    payload.task_id,
                    other_id,
                    other_actor,
                    overlap,
                )
                continue
            raise EventRejected(
                f"claim.created: concurrency guard failed for task "
                f"'{payload.task_id}'. expected_files overlap active claim "
                f"'{other_id}' by '{other_actor}' (files: {overlap}). Another "
                "claim acquired these files first; re-pick a task or use "
                "--force to override."
            )

    def _check_claim_group_overlap(
        self,
        conn: sqlite3.Connection,
        payload: ClaimCreatedPayload,
    ) -> None:
        """Reject the claim if its task shares a conflict_group with an active claim.

        Second half of the TOCTOU fix: ``ClaimManager._check_group_conflicts``
        runs OUTSIDE the serialized write path, so two concurrent claims on two
        *different* tasks that share a conflict_group can both pass that
        pre-check and both commit. This re-check runs inside the same
        flock + BEGIN IMMEDIATE critical section, comparing the claimed task's
        conflict_groups against the conflict_groups of every task that already
        holds a DIFFERENT active claim. A non-empty intersection is a conflict —
        rejected unless ``payload.force`` overrides it (mirroring the pre-check's
        warn-and-proceed behaviour).
        """
        group_row = conn.execute(
            "SELECT conflict_groups FROM tasks WHERE id = ?", (payload.task_id,)
        ).fetchone()
        if group_row is None:
            return
        try:
            my_groups: set[str] = {str(g) for g in json.loads(group_row[0] or "[]")}
        except (json.JSONDecodeError, TypeError):
            return
        if not my_groups:
            return

        # Active claims on OTHER tasks, joined to those tasks' conflict_groups.
        rows = conn.execute(
            """
            SELECT c.id, c.claimed_by, c.task_id, t.conflict_groups
              FROM claims c
              JOIN tasks t ON t.id = c.task_id
             WHERE c.status = 'active'
            """
        ).fetchall()

        for row in rows:
            other_claim_id = row[0]
            other_actor = row[1]
            other_task_id = row[2]
            if other_task_id == payload.task_id:
                continue
            try:
                other_groups = {str(g) for g in json.loads(row[3] or "[]")}
            except (json.JSONDecodeError, TypeError):
                continue
            shared = sorted(my_groups & other_groups)
            if not shared:
                continue
            if payload.force:
                logger.warning(
                    "Forced claim %r on task %r: in-transaction conflict_group "
                    "overlap with active claim %r on task %r by %r "
                    "(groups: %r) — overridden.",
                    payload.id,
                    payload.task_id,
                    other_claim_id,
                    other_task_id,
                    other_actor,
                    shared,
                )
                continue
            raise EventRejected(
                f"claim.created: concurrency guard failed for task "
                f"'{payload.task_id}'. conflict_group overlap (groups: {shared}) "
                f"with active claim '{other_claim_id}' on task '{other_task_id}' "
                f"by '{other_actor}'. Another claim in this group is active; "
                "re-pick a task or use --force to override."
            )

    def _write_claim_created(
        self,
        conn: sqlite3.Connection,
        payload: ClaimCreatedPayload,
        event: Event,
    ) -> None:
        """INSERT the claim and transition the task ready → claimed.

        Payload fields (all required):
            id (str)                — claim PK
            task_id (str)          — FK into tasks
            claimed_by (str)       — agent identifier
            claim_type (str)       — ClaimType value
            status (str)           — must be 'active' for a new claim
            branch (str | None)
            worktree_path (str | None)
            expected_files (list[str])
            created_at (str)       — ISO 8601 UTC
            lease_expires_at (str) — ISO 8601 UTC
            last_heartbeat_at (str) — ISO 8601 UTC

        Idempotent: INSERT OR IGNORE on the claim id PK — replay is safe. The
        task UPDATE keeps its WHERE status='ready' guard; when the task is
        already 'claimed' (replay of a committed claim) it matches 0 rows
        harmlessly, which ``_check_claim_created`` already determined is
        acceptable.
        """
        claim_id: str = payload.id
        task_id: str = payload.task_id
        claimed_by: str = payload.claimed_by
        claim_type: str = payload.claim_type
        status: str = payload.status
        created_at: str = payload.created_at
        lease_expires_at: str = payload.lease_expires_at
        last_heartbeat_at: str = payload.last_heartbeat_at
        branch: str | None = payload.branch
        worktree_path: str | None = payload.worktree_path
        expected_files = payload.expected_files
        timestamp: str = event.timestamp.astimezone(datetime.UTC).isoformat()

        if event.target_kind != "claim" or event.target_id != claim_id:
            return

        # A claim ID identifies one immutable creation fact. Merged branches
        # can independently reuse an ID for different tasks; the first
        # HLC-ordered fact wins, and a losing duplicate must have no projection
        # side effects on either its task or a competing bundle.
        existing_claim = conn.execute(
            "SELECT task_id FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if existing_claim is not None:
            fingerprint = self._claim_creation_fingerprint(payload)
            lineage = conn.execute(
                "SELECT creation_fingerprint FROM claim_replay_lineages "
                "WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if lineage is None:
                conn.execute(
                    "INSERT INTO claim_replay_lineages "
                    "(claim_id, creation_fingerprint) VALUES (?, ?)",
                    (claim_id, fingerprint),
                )
            elif lineage[0] != fingerprint:
                conn.execute(
                    "UPDATE claim_replay_lineages SET collision_detected = 1 "
                    "WHERE claim_id = ?",
                    (claim_id,),
                )
            return

        # Replay is write-only. Preserve first-writer exclusion when a prior
        # standalone or internal bundle authorization already owns this task;
        # losing claim descendants fail closed because their claim row is absent.
        if conn.execute(
            "SELECT 1 FROM claims WHERE task_id = ? AND status = 'active' "
            "AND bundle_claim_id IS NOT NULL LIMIT 1",
            (task_id,),
        ).fetchone():
            return
        incoming_files = set(payload.expected_files or [])
        task_group_row = conn.execute(
            "SELECT conflict_groups FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        incoming_groups = set(
            json.loads(task_group_row[0] or "[]") if task_group_row else []
        )
        for bundle_child in conn.execute(
            "SELECT c.expected_files, t.conflict_groups FROM claims c "
            "JOIN tasks t ON t.id = c.task_id WHERE c.status = 'active' "
            "AND c.bundle_claim_id IS NOT NULL"
        ).fetchall():
            if incoming_files.intersection(json.loads(bundle_child[0] or "[]")):
                return
            if incoming_groups.intersection(json.loads(bundle_child[1] or "[]")):
                return

        terminal = tuple(bundle_status.value for bundle_status in TERMINAL_BUNDLE_STATUSES)
        terminal_placeholders = ",".join("?" for _ in terminal)
        # Git branches can independently create a bundle and claim one of its
        # tasks. The claim may already have evidence/review descendants, so
        # dropping it would poison replay on those FKs. Deterministically let
        # the complete claim lineage win in merged history: retire competing
        # bundles, then project the claim normally. Live append still rejects
        # this ordering in _validate_claim_created_locked.
        conn.execute(
            f"""UPDATE execution_bundles
                   SET status = 'superseded',
                       updated_at = CASE
                           WHEN updated_at < ? THEN ?
                           ELSE updated_at
                       END
                 WHERE id IN (
                    SELECT b.id
                      FROM execution_bundle_members m
                      JOIN execution_bundles b ON b.id = m.bundle_id
                     WHERE m.task_id = ?
                       AND b.status NOT IN ({terminal_placeholders})
                 )""",
            (timestamp, timestamp, task_id) + terminal,
        )

        # INSERT OR IGNORE: idempotent on replay — duplicate claim.created events
        # (after crash mid-transaction) do not produce duplicate rows.
        conn.execute(
            """
            INSERT OR IGNORE INTO claims
                (id, task_id, claimed_by, claim_type, status, branch,
                 worktree_path, session_id, expected_files, created_at,
                 lease_expires_at, last_heartbeat_at, released_at, release_reason)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                claim_id,
                task_id,
                claimed_by,
                claim_type,
                status,
                branch,
                worktree_path,
                getattr(payload, "session_id", None),
                json.dumps(expected_files),
                created_at,
                lease_expires_at,
                last_heartbeat_at,
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO claim_replay_lineages "
            "(claim_id, creation_fingerprint) VALUES (?, ?)",
            (claim_id, self._claim_creation_fingerprint(payload)),
        )

        # Side-effect: transition the task status from 'ready' → 'claimed'.
        # The WHERE status='ready' guard is preserved; on an already-claimed
        # task it matches 0 rows (acceptable per the check) and on a fresh ready
        # task it promotes to claimed.
        conn.execute(
            """
            UPDATE tasks
               SET status = 'claimed',
                   updated_at = ?
             WHERE id = ?
               AND status = 'ready'
            """,
            (timestamp, task_id),
        )

    @staticmethod
    def _claim_release_target(force: bool) -> tuple[str, str]:
        """Return (target_status, SQL status_guard) for a claim release (pure).

        Force-release writes a distinct terminal status (force_released) so the
        audit trail captures the override; normal release uses 'released'. Force
        also allows non-active claims to be released so a stranded stale claim
        can be cleaned up after the fact.
        """
        if force:
            return "force_released", "status NOT IN ('released', 'force_released')"
        return "released", "status = 'active'"

    def _check_claim_released(
        self,
        conn: sqlite3.Connection,
        payload: ClaimReleasedPayload,
        event: EventDraft,
    ) -> None:
        """Decide whether a claim release mutates or is an idempotent no-op.

        The old handler ran the guarded UPDATE then on a 0-row result: claim
        not found → TransactionAborted; already-terminal → warn.idempotent_no_op
        + silent return. This check reproduces those on read-only state: reject
        a missing claim, or signal an ``IdempotentNoOp`` (carrying the warn-log
        metadata so the prior tombstone is still emitted) for an already-terminal
        claim that the guard would not match.
        """
        _ = event
        claim_id: str = payload.claim_id
        self._reject_claim_replay_collision(conn, claim_id, "claim.released")
        row = conn.execute(
            "SELECT status, bundle_claim_id FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if row is None:
            raise EventRejected(f"claim.released: claim '{claim_id}' not found.")
        if row[1] is not None:
            raise EventRejected(
                "claim.released: bundle member authorizations are managed by "
                "their coordinator claim."
            )
        current_status = row[0]
        if payload.force:
            guard_matches = current_status not in ("released", "force_released")
        else:
            guard_matches = current_status == "active"
        if not guard_matches:
            # Already terminal — idempotent no-op; the old handler emitted a
            # warn.idempotent_no_op line and returned without raising.
            raise _idempotent_no_op(
                f"claim.released: claim '{claim_id}' already has status "
                f"'{current_status}'; treating as idempotent no-op.",
                warn_action="claim.released",
                warn_target_id=claim_id or "",
            )

    def _write_claim_released(
        self,
        conn: sqlite3.Connection,
        payload: ClaimReleasedPayload,
        event: Event,
    ) -> None:
        """Release the claim and return its task to 'ready'.

        Payload fields (all required):
            claim_id (str)       — PK of the claim to release
            released_by (str)    — agent releasing the claim
            release_reason (str) — human-readable reason

        ``_check_claim_released`` already proved the guard matches, so the
        claims UPDATE always hits a row. The task UPDATE keeps its widened
        WHERE-status guard and tolerates 0 rows (the task may have legitimately
        advanced).
        """
        claim_id: str = payload.claim_id
        child_row = conn.execute(
            "SELECT bundle_claim_id FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if child_row is not None and child_row[0] is not None:
            return
        if self._claim_replay_collision(conn, claim_id):
            return
        release_reason: str | None = payload.release_reason
        force: bool = payload.force
        timestamp: str = event.timestamp.isoformat()

        target_status, status_guard = self._claim_release_target(force)

        conn.execute(
            f"""
            UPDATE claims
               SET status = ?,
                   released_at = ?,
                   release_reason = ?
             WHERE id = ?
               AND {status_guard}
            """,  # noqa: S608 — status_guard is a literal, not user input
            (target_status, timestamp, release_reason, claim_id),
        )

        # Side-effect: return the task to 'ready'. Widened from the original
        # WHERE status='claimed' (which would TransactionAborted on tasks that
        # had advanced to in_progress or blocked) to all post-claim, pre-done
        # statuses. Critic flagged this: release --force is supposed to work
        # even when the task has progressed mid-work.
        task_row = conn.execute(
            "SELECT task_id FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if task_row is not None:
            task_id = task_row[0]
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'ready',
                       updated_at = ?
                 WHERE id = ?
                   AND status IN ('claimed', 'in_progress', 'blocked')
                """,
                (timestamp, task_id),
            )
            # 0 rows is now acceptable: the task may have legitimately advanced
            # to needs_review, accepted, or done in parallel (Phase 5 completion).
            # No error — releasing the claim is the right behaviour regardless.

    def _check_claim_renewed(
        self,
        conn: sqlite3.Connection,
        payload: ClaimRenewedPayload,
        event: EventDraft,
    ) -> None:
        """Confirm the claim exists and is active before extending its lease.

        Was two validation guards in the old handler (claim not found; claim not
        active) interpreted from a 0-row UPDATE; both now reject up front.
        """
        _ = event
        claim_id: str = payload.claim_id
        self._reject_claim_replay_collision(conn, claim_id, "claim.renewed")
        row = conn.execute(
            "SELECT status, bundle_claim_id FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if row is None:
            raise EventRejected(f"claim.renewed: claim '{claim_id}' not found.")
        if row[1] is not None:
            raise EventRejected(
                "claim.renewed: renew the public bundle coordinator claim instead."
            )
        actual_status = row[0]
        if actual_status != "active":
            raise EventRejected(
                f"claim.renewed: cannot renew claim '{claim_id}' "
                f"with status '{actual_status}' (must be 'active')."
            )

    def _write_claim_renewed(
        self,
        conn: sqlite3.Connection,
        payload: ClaimRenewedPayload,
        event: Event,
    ) -> None:
        """Extend the lease on an active claim.

        Payload fields (all required):
            claim_id (str)          — PK of the claim to renew
            lease_expires_at (str)  — new expiry (ISO 8601 UTC)
            last_heartbeat_at (str) — updated heartbeat timestamp (ISO 8601 UTC)

        Does NOT mutate the tasks table.

        ``_check_claim_renewed`` already proved the claim exists and is active,
        so the WHERE status='active' UPDATE always hits a row.

        The event-level timestamp is not used here — the renewed lease timestamps
        come from the payload itself.
        """
        claim_id: str = payload.claim_id
        child_row = conn.execute(
            "SELECT bundle_claim_id FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if child_row is not None and child_row[0] is not None:
            return
        if self._claim_replay_collision(conn, claim_id):
            return
        _ = event
        lease_expires_at: str = payload.lease_expires_at
        last_heartbeat_at: str = payload.last_heartbeat_at

        conn.execute(
            """
            UPDATE claims
               SET lease_expires_at = ?,
                   last_heartbeat_at = ?
             WHERE id = ?
               AND status = 'active'
            """,
            (lease_expires_at, last_heartbeat_at, claim_id),
        )

    def _check_claim_stale(
        self,
        conn: sqlite3.Connection,
        payload: ClaimStalePayload,
        event: EventDraft,
    ) -> None:
        """Decide whether marking a claim stale mutates or is a no-op.

        The old handler ran the guarded UPDATE then on a 0-row result: claim not
        found → TransactionAborted; not active (already stale / terminal) →
        warn.idempotent_no_op + silent return. This check reproduces those on
        read-only state: reject a missing claim, or signal an ``IdempotentNoOp``
        (carrying warn-log metadata) for a non-active claim.
        """
        _ = event
        claim_id: str = payload.claim_id
        self._reject_claim_replay_collision(conn, claim_id, "claim.stale")
        row = conn.execute(
            "SELECT status, bundle_claim_id FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if row is None:
            raise EventRejected(f"claim.stale: claim '{claim_id}' not found.")
        if row[1] is not None:
            raise EventRejected(
                "claim.stale: reap the public bundle coordinator claim instead."
            )
        current_status = row[0]
        if current_status != "active":
            raise _idempotent_no_op(
                f"claim.stale: claim '{claim_id}' already has status "
                f"'{current_status}'; treating as idempotent no-op.",
                warn_action="claim.stale",
                warn_target_id=claim_id or "",
            )

    def _write_claim_stale(
        self,
        conn: sqlite3.Connection,
        payload: ClaimStalePayload,
        event: Event,
    ) -> None:
        """Mark an active claim as stale and return the task to 'ready'.

        Payload fields (all required):
            claim_id (str)    — PK of the claim to mark stale
            detected_at (str) — when staleness was detected (ISO 8601 UTC)
            reason (str)      — typically 'lease_expired'

        ``_check_claim_stale`` already proved the claim is active, so the WHERE
        status='active' UPDATE always hits a row.

        Side-effect: UPDATE tasks SET status='ready' WHERE id=task_id AND
        status IN ('claimed', 'in_progress', 'blocked').  If the task status
        has already moved beyond those states (e.g., accepted, done), the
        UPDATE is a no-op — that is intentional and not an error.
        """
        claim_id: str = payload.claim_id
        child_row = conn.execute(
            "SELECT bundle_claim_id FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if child_row is not None and child_row[0] is not None:
            return
        if self._claim_replay_collision(conn, claim_id):
            return
        timestamp: str = event.timestamp.isoformat()

        conn.execute(
            """
            UPDATE claims
               SET status = 'stale',
                   released_at = ?,
                   release_reason = 'lease_expired'
             WHERE id = ?
               AND status = 'active'
            """,
            (timestamp, claim_id),
        )

        # Side-effect: return the task to 'ready' if it is still in an
        # active-work status.  Tasks already at accepted/done/rejected are
        # left untouched — the work completed before the lease expired.
        task_row = conn.execute(
            "SELECT task_id FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if task_row is not None:
            task_id = task_row[0]
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'ready',
                       updated_at = ?
                 WHERE id = ?
                   AND status IN ('claimed', 'in_progress', 'blocked')
                """,
                (timestamp, task_id),
            )
            # No error if 0 rows — the task may have been completed already.

    # ------------------------------------------------------------------
    # Phase 5 handlers — completion flow
    # ------------------------------------------------------------------

    def _check_evidence_submitted(
        self,
        conn: sqlite3.Connection,
        payload: EvidenceSubmittedPayload,
        event: EventDraft,
    ) -> None:
        """Validate the evidence payload and decide the submission outcome.

        Reproduces the old handler's pre-mutation guards on read-only state:

        - Empty ``commands_run`` / ``files_changed`` → reject (was
          TransactionAborted).
        - CL-8 double-submit (this claim already carries evidence under a
          *different* id) → ``IdempotentNoOp`` carrying the warn-log metadata, so
          the prior warn.idempotent_no_op tombstone is still emitted and nothing
          mutates.
        - Task missing → reject; task not in an eligible status and not already
          ``needs_review`` → reject (was the 0-row TransactionAborted).

        The claim auto-release branch (and its conditional warn log) is NOT a
        gate — it is a side effect of the write and stays there.
        """
        _ = event
        # At least one proof is mandatory: a non-empty `commands_run`. An empty
        # `files_changed` is legitimate for a verification-only / check step that
        # runs commands but changes no files (B32) — the older rule forced a
        # "(none)" placeholder. Honesty is preserved: a "done" with zero commands
        # is still rejected.
        if not payload.commands_run:
            raise EventRejected(
                "evidence.submitted payload requires non-empty 'commands_run'."
            )

        claim_id: str = payload.claim_id
        evidence_id: str = payload.evidence_id
        task_id: str = payload.task_id

        claim_row = conn.execute(
            "SELECT task_id FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if claim_row is None:
            raise EventRejected(
                f"evidence.submitted: claim '{claim_id}' not found."
            )
        if claim_row[0] != task_id:
            raise EventRejected(
                f"evidence.submitted: claim '{claim_id}' belongs to task "
                f"'{claim_row[0]}', not '{task_id}'."
            )
        active_bundle_child = conn.execute(
            "SELECT id FROM claims WHERE task_id = ? AND status = 'active' "
            "AND bundle_claim_id IS NOT NULL",
            (task_id,),
        ).fetchone()
        if active_bundle_child is not None and active_bundle_child[0] != claim_id:
            raise EventRejected(
                "evidence.submitted: task is owned by a bundle member claim; "
                f"expected '{active_bundle_child[0]}'."
            )
        self._reject_claim_replay_collision(conn, claim_id, "evidence.submitted")

        # CL-8 idempotency guard: a second submit under a DIFFERENT evidence_id
        # is a double-submit; the old handler emitted a warn line and returned.
        existing_row = conn.execute(
            "SELECT id FROM evidence WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()
        if existing_row is not None and existing_row[0] != evidence_id:
            raise _idempotent_no_op(
                f"evidence.submitted: claim '{claim_id}' already has evidence "
                f"'{existing_row[0]}'; rejecting duplicate submission with new "
                f"evidence_id '{evidence_id}' as idempotent no-op (CL-8).",
                warn_action="evidence.submitted",
                warn_target_id=claim_id or "",
            )

        # Task eligibility: must exist and be in an active-work status, or be
        # already at needs_review (idempotent replay — write's task UPDATE will
        # no-op harmlessly).
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise EventRejected(
                f"evidence.submitted: task '{task_id}' not found."
            )
        actual_status = row[0]
        if actual_status not in (
            "claimed",
            "in_progress",
            "blocked",
            "needs_review",
        ):
            raise EventRejected(
                f"evidence.submitted: task '{task_id}' has status "
                f"'{actual_status}', which is not eligible for evidence submission "
                "(must be 'claimed', 'in_progress', or 'blocked')."
            )

    def _write_evidence_submitted(
        self,
        conn: sqlite3.Connection,
        payload: EvidenceSubmittedPayload,
        event: Event,
    ) -> None:
        """Insert evidence, transition task to needs_review, auto-release claim.

        Payload fields:
            task_id (str)            — required; FK into tasks
            claim_id (str)           — required; FK into claims
            submitted_by (str)       — required; agent identifier
            evidence_id (str)        — required; PK for the evidence row
            commands_run (list[str]) — required; must be non-empty
            files_changed (list[str])— required
            output_excerpt (str | None) — optional
            pr_url (str | None)         — optional
            commit_sha (str | None)     — optional
            screenshots (list[str])     — optional, default []
            known_limitations (str | None) — optional

        ``_check_evidence_submitted`` already validated the payload, screened the
        CL-8 double-submit, and proved task eligibility. The mutations here are
        infallible:
            - INSERT OR IGNORE on evidence.id PK — idempotent on replay.
            - Task UPDATE (claimed/in_progress/blocked → needs_review) matches a
              row, or no-ops when the task was already needs_review.
            - Claim auto-release (UPDATE active → released). 0 rows means the
              claim was already released/stale; the conditional warn log records
              that — it is an audit side effect, not a rejection.
        """
        task_id: str = payload.task_id
        claim_id: str = payload.claim_id
        claim_row = conn.execute(
            "SELECT task_id FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if (
            claim_row is None
            or claim_row[0] != task_id
            or self._claim_replay_collision(conn, claim_id)
        ):
            return
        submitted_by: str = payload.submitted_by
        evidence_id: str = payload.evidence_id
        commands_run: list[Any] = payload.commands_run
        files_changed: list[Any] = payload.files_changed
        timestamp: str = event.timestamp.isoformat()

        output_excerpt: str | None = payload.output_excerpt
        pr_url: str | None = payload.pr_url
        commit_sha: str | None = payload.commit_sha
        screenshots: list[Any] = payload.screenshots or []
        known_limitations: str | None = payload.known_limitations
        # SL-3 / B48: serialize the typed proofs to the JSON column. The payload
        # already validated each proof against the discriminated union, so
        # model_dump(mode="json") round-trips losslessly.
        proofs_json = json.dumps([p.model_dump(mode="json") for p in payload.proofs])

        # INSERT OR IGNORE: idempotent on replay — duplicate evidence.submitted
        # events (after crash mid-transaction) do not produce duplicate rows.
        conn.execute(
            """
            INSERT OR IGNORE INTO evidence
                (id, task_id, claim_id, commands_run, output_excerpt,
                 files_changed, pr_url, commit_sha, screenshots,
                 known_limitations, proofs, category, submitted_at,
                 submitted_by)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                task_id,
                claim_id,
                json.dumps(commands_run),
                output_excerpt,
                json.dumps(files_changed),
                pr_url,
                commit_sha,
                json.dumps(screenshots),
                known_limitations,
                proofs_json,
                payload.category or "completion",
                timestamp,
                submitted_by,
            ),
        )

        # Atomically transition the task to needs_review.
        # WHERE status IN ('claimed', 'in_progress', 'blocked') is the
        # concurrency guard — allows submit from any active-work status.
        # ``_check_evidence_submitted`` already proved the task is in one of
        # those statuses or already at needs_review, so a 0-row result here is
        # the harmless idempotent-replay case (no raise needed).
        conn.execute(
            """
            UPDATE tasks
               SET status = 'needs_review',
                   updated_at = ?
             WHERE id = ?
               AND status IN ('claimed', 'in_progress', 'blocked')
            """,
            (timestamp, task_id),
        )

        # Auto-release the active claim.
        conn.execute(
            """
            UPDATE claims
               SET status = 'released',
                   released_at = ?,
                   release_reason = 'auto-released on submit'
             WHERE id = ?
               AND status = 'active'
            """,
            (timestamp, claim_id),
        )

        if conn.execute("SELECT changes()").fetchone()[0] == 0:
            # Claim already released or stale — idempotent; log warning to audit.jsonl
            # only during normal (non-replay) execution. During replay/catch-up the
            # audit line was already written on the first run; re-writing it would
            # cause unbounded audit.jsonl growth contradicting the "no logging during
            # replay" contract.
            if not self._replaying:
                row = conn.execute(
                    "SELECT status FROM claims WHERE id = ?", (claim_id,)
                ).fetchone()
                current_status = row[0] if row else "not found"
                self._write_warn_to_audit(
                    action="evidence.submitted",
                    target_id=claim_id or "",
                    reason=(
                        f"evidence.submitted: claim '{claim_id}' auto-release skipped; "
                        f"current status is '{current_status}'."
                    ),
                )

    def _check_task_applied(
        self,
        conn: sqlite3.Connection,
        payload: TaskAppliedPayload,
        event: EventDraft,
    ) -> None:
        """Validate the decision and the task's eligibility for it.

        Reproduces the old handler's pre-mutation guards on read-only state:

        - ``decision`` must be 'accepted' or 'rejected' → reject.
        - Task must exist → reject.
        - Task status must be ``needs_review`` (fresh apply) or, for replay
          idempotency, already in the decision's terminal set
          (accepted/done for accept, rejected/drafted for reject) → otherwise
          reject as status-drift.

        The accepted → done / rejected → drafted auto-promotion (and its
        defensive 0-row invariant) is a write-phase concern, left in
        ``_write_task_applied``.
        """
        _ = event
        decision: str = payload.decision
        task_id: str = payload.task_id

        if decision not in ("accepted", "rejected"):
            raise EventRejected(
                f"task.applied: 'decision' must be 'accepted' or 'rejected', "
                f"got {decision!r}."
            )

        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise EventRejected(f"task.applied: task '{task_id}' not found.")
        actual_status = row[0]
        if actual_status == "needs_review":
            return  # fresh apply — proceed.
        acceptable = (
            ("accepted", "done")
            if decision == "accepted"
            else ("rejected", "drafted")
        )
        if actual_status not in acceptable:
            raise EventRejected(
                f"task.applied: status-drift for task '{task_id}'. "
                f"Expected 'needs_review', got '{actual_status}'. "
                "The task may have been reviewed by a concurrent operation."
            )

    def _write_task_applied(
        self,
        conn: sqlite3.Connection,
        payload: TaskAppliedPayload,
        event: Event,
    ) -> None:
        """Gate needs_review → accepted → done (or rejected) and record a Review.

        Payload fields:
            task_id (str)              — required
            reviewer (str)             — required
            decision (str)             — required; 'accepted' or 'rejected'
            notes (str | None)         — optional

        Transition logic:
            - decision='accepted': UPDATE tasks SET status='accepted' WHERE
              status='needs_review', then immediately UPDATE tasks SET
              status='done'. These two mutations are committed in the same
              transaction (accepted → done is automatic; they are never split).
            - decision='rejected': UPDATE tasks SET status='rejected' WHERE
              status='needs_review'.

        Review row:
            INSERT OR REPLACE INTO reviews with id=f"RV-{event_id}" for
            replay safety. target_kind='task', target_id=task_id.

        ``_check_task_applied`` already validated the decision and proved the
        task is at needs_review (or in the idempotent-replay terminal set). The
        WHERE-status guards and 0-row branches remain as a defensive belt that
        handles the idempotent-replay no-op without raising; the only surviving
        raise is the accepted → done auto-promote invariant, which signals a
        genuine unexpected concurrent mutation (infra-class, not a validation
        rejection).
        """
        task_id: str = payload.task_id
        reviewer: str = payload.reviewer
        decision: str = payload.decision
        notes: str | None = payload.notes
        event_id: str = event.id
        timestamp: str = event.timestamp.isoformat()

        if decision == "accepted":
            # Transition needs_review → accepted.
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'accepted',
                       updated_at = ?
                 WHERE id = ?
                   AND status = 'needs_review'
                """,
                (timestamp, task_id),
            )
            if conn.execute("SELECT changes()").fetchone()[0] == 0:
                row = conn.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise TransactionAborted(
                        f"task.applied: task '{task_id}' not found."
                    )
                actual_status = row[0]
                # Idempotent replay: already accepted or done.
                if actual_status not in ("accepted", "done"):
                    raise TransactionAborted(
                        f"task.applied: status-drift for task '{task_id}'. "
                        f"Expected 'needs_review', got '{actual_status}'. "
                        "The task may have been reviewed by a concurrent operation."
                    )
            else:
                # Immediately promote accepted → done in the same transaction.
                conn.execute(
                    """
                    UPDATE tasks
                       SET status = 'done',
                           updated_at = ?
                     WHERE id = ?
                       AND status = 'accepted'
                    """,
                    (timestamp, task_id),
                )
                # accepted → done is an automatic follow-up; 0 rows here would
                # be a logic error (we just set it to 'accepted' above), so we
                # do raise — it signals an unexpected concurrent mutation.
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise TransactionAborted(
                        f"task.applied: failed to auto-promote task '{task_id}' "
                        "from 'accepted' to 'done'. Unexpected concurrent mutation."
                    )

        else:  # decision == "rejected"
            # Per spec: needs_review → rejected → drafted (automatic; same txn).
            # The 'rejected' state is a brief audit marker; the task immediately
            # transitions to 'drafted' so it can be re-reviewed and re-promoted.
            # Critic-1 + Critic-2 both flagged that the original code left the
            # task permanently at 'rejected' with no path back, contradicting
            # docs/specs/2026-05-24-anvil-v0.md and skills/finish/SKILL.md.
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'rejected',
                       updated_at = ?
                 WHERE id = ?
                   AND status = 'needs_review'
                """,
                (timestamp, task_id),
            )
            if conn.execute("SELECT changes()").fetchone()[0] == 0:
                row = conn.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise TransactionAborted(
                        f"task.applied: task '{task_id}' not found."
                    )
                actual_status = row[0]
                # Idempotent replay: either we already rejected (transient
                # marker before drafted) or already drafted (final state).
                if actual_status not in ("rejected", "drafted"):
                    raise TransactionAborted(
                        f"task.applied: status-drift for task '{task_id}'. "
                        f"Expected 'needs_review', got '{actual_status}'. "
                        "The task may have been reviewed by a concurrent operation."
                    )
            else:
                # Initial run (not replay): auto-promote rejected → drafted
                # in the same transaction. The audit log carries 'rejected'
                # as the recorded decision; the task lifecycle continues
                # at 'drafted' so it can be re-reviewed.
                conn.execute(
                    """
                    UPDATE tasks
                       SET status = 'drafted',
                           updated_at = ?
                     WHERE id = ?
                       AND status = 'rejected'
                    """,
                    (timestamp, task_id),
                )

        # Insert the Review row — INSERT OR REPLACE for replay safety.
        review_id = f"RV-{event_id}"
        conn.execute(
            """
            INSERT OR REPLACE INTO reviews
                (id, target_kind, target_id, reviewed_by, decision, notes, created_at)
            VALUES
                (?, 'task', ?, ?, ?, ?, ?)
            """,
            (review_id, task_id, reviewer, decision, notes, timestamp),
        )

    # ------------------------------------------------------------------
    # Phase 8 handlers — sync_mappings (external-system mirror)
    # ------------------------------------------------------------------

    def _check_sync_mapping_upserted(
        self,
        conn: sqlite3.Connection,
        payload: SyncMappingUpsertedPayload,
        event: EventDraft,
    ) -> None:
        """Validate the SyncMapping (enum / UTC checks) before the upsert.

        Was a validation guard inside the old handler (``raise
        TransactionAborted`` on an invalid SyncMapping); now rejects up front.

        Note on ``entity_kind='prd'`` (milestone) mappings: the model + validator
        accept a prd-kind shape (``task_id=None``), but the ``sync_mappings`` DDL
        still declares ``task_id TEXT NOT NULL`` (T026 is the data-only phase; the
        column is relaxed in the milestone phase). Persisting a prd-kind row would
        therefore abort the write transaction with an opaque NOT NULL
        ``IntegrityError`` inside the lock — exactly the deferred-failure mode this
        gate exists to prevent. So reject prd-kind events here, at the call site,
        rather than mid-write/mid-replay.
        """
        _ = (conn, event)
        try:
            SyncMapping.model_validate(payload.model_dump())
        except Exception as exc:
            raise EventRejected(
                f"sync_mapping.upserted: invalid SyncMapping payload: {exc}"
            ) from exc
        if payload.entity_kind == "prd":
            raise EventRejected(
                "sync_mapping.upserted: entity_kind='prd' (milestone) mappings "
                "are not yet persistable — sync_mappings.task_id is still "
                "NOT NULL (deferred to the milestone phase)"
            )

    def _write_sync_mapping_upserted(
        self,
        conn: sqlite3.Connection,
        payload: SyncMappingUpsertedPayload,
        event: Event,
    ) -> None:
        """Insert a sync_mappings row, or UPDATE on (task_id, external_system) conflict.

        The composite primary key is (task_id, external_system), so a task that
        is mirrored into two external systems gets two rows — the upsert keys
        on the full PK, not on task_id alone. This is intentional: a task can
        legitimately have a github_issues mapping AND, in the future, a
        linear mapping, both kept in sync.

        ``_check_sync_mapping_upserted`` already validated the payload; the
        ``model_validate`` here is an infallible rebuild that yields the
        canonical serialized form.
        """
        mapping = SyncMapping.model_validate(payload.model_dump())

        # Use the validated model's serialized form so enum values become the
        # canonical string. last_synced_at is already an ISO string from the
        # payload model.
        data = mapping.model_dump(mode="json")
        _ = event  # event-level timestamp not used; mapping carries last_synced_at
        # provider_metadata is opaque dict — serialise to JSON for the
        # provider_metadata_json TEXT column.
        provider_metadata_json = json.dumps(data.get("provider_metadata") or {})
        # prd_id / entity_kind are exclude=True on SyncMapping, so they are NOT
        # in ``data`` — read the persisted partition straight off the payload
        # (which defaults to prd_id='default' / entity_kind='task' for a
        # pre-change event, matching the v6->v7 migration backfill).
        conn.execute(
            """
            INSERT INTO sync_mappings
                (task_id, external_system, external_id, external_url,
                 last_synced_at, sync_state, conflict_resolution_strategy,
                 provider_metadata_json, prd_id, entity_kind)
            VALUES
                (:task_id, :external_system, :external_id, :external_url,
                 :last_synced_at, :sync_state, :conflict_resolution_strategy,
                 :provider_metadata_json, :prd_id, :entity_kind)
            ON CONFLICT(task_id, external_system) DO UPDATE SET
                external_id                  = excluded.external_id,
                external_url                 = excluded.external_url,
                last_synced_at               = excluded.last_synced_at,
                sync_state                   = excluded.sync_state,
                conflict_resolution_strategy = excluded.conflict_resolution_strategy,
                provider_metadata_json       = excluded.provider_metadata_json,
                prd_id                       = excluded.prd_id,
                entity_kind                  = excluded.entity_kind
            """,
            {
                "task_id": data["task_id"],
                "external_system": data["external_system"],
                "external_id": data["external_id"],
                "external_url": data.get("external_url"),
                "last_synced_at": data["last_synced_at"],
                "sync_state": data["sync_state"],
                "conflict_resolution_strategy": data["conflict_resolution_strategy"],
                "provider_metadata_json": provider_metadata_json,
                "prd_id": payload.prd_id,
                "entity_kind": payload.entity_kind,
            },
        )

    def _check_sync_mapping_deleted(
        self,
        conn: sqlite3.Connection,
        payload: SyncMappingDeletedPayload,
        event: EventDraft,
    ) -> None:
        """No validation gate — sync_mapping.deleted is an idempotent delete."""
        _ = (conn, payload, event)

    def _write_sync_mapping_deleted(
        self,
        conn: sqlite3.Connection,
        payload: SyncMappingDeletedPayload,
        event: Event,
    ) -> None:
        """Delete sync_mappings row(s) for ``task_id``.

        If ``external_system`` is provided the delete is scoped to that single
        row (composite-key delete). If absent, every mapping for the task is
        removed — supports the "untrack everything" case.

        Idempotent: a delete against an already-absent row is a silent no-op.
        Audit visibility comes from the event row itself, recorded by the
        caller in the events table.
        """
        _ = event  # event-level timestamp not used; delete carries no audit field
        if payload.external_system is None:
            conn.execute(
                "DELETE FROM sync_mappings WHERE task_id = ?",
                (payload.task_id,),
            )
        else:
            conn.execute(
                "DELETE FROM sync_mappings WHERE task_id = ? AND external_system = ?",
                (payload.task_id, payload.external_system),
            )

    # ------------------------------------------------------------------
    # Internal helpers — task row insertion (shared by task.created and
    # task.expanded)
    # ------------------------------------------------------------------

    def _insert_task_row(self, conn: sqlite3.Connection, task: Task) -> None:
        """Insert or upsert a Task row in the tasks table.

        Uses INSERT ... ON CONFLICT DO UPDATE (not INSERT OR REPLACE) for the
        same reason as feature.created: INSERT OR REPLACE is DELETE + INSERT,
        which trips ON DELETE RESTRICT on claims.task_id and evidence.task_id
        if anything has been claimed against this task. The upsert pattern
        preserves the row identity, so foreign keys remain valid even when
        `plan` is re-run after work has begun.
        """
        data = task.model_dump(mode="json")
        # prd_id is Field(exclude=True) — read it as the in-memory attribute and
        # write the column explicitly (pre-v7 replay carries 'default').
        prd_id = task.prd_id
        # Denormalization invariant: a task lives in its owning feature's PRD
        # partition. Assert Task.prd_id == owning Feature.prd_id at write time so
        # a cross-partition mismatch fails fast here rather than silently
        # producing an orphaned partition. The owning feature row exists by the
        # time a task is written (feature.created precedes task.created in both
        # the live path and replay ordering); if it is somehow absent we skip the
        # check rather than invent a constraint.
        feat_row = conn.execute(
            "SELECT prd_id FROM features WHERE id = ?", (task.feature_id,)
        ).fetchone()
        if feat_row is not None and feat_row[0] != prd_id:
            raise TransactionAborted(
                f"task {task.id!r} prd_id={prd_id!r} does not match owning "
                f"feature {task.feature_id!r} prd_id={feat_row[0]!r}"
            )
        conn.execute(
            """
            INSERT INTO tasks
                (id, prd_id, feature_id, title, description, status, priority,
                 task_type, dependencies, conflict_groups, scores,
                 acceptance_criteria, implementation_notes, verification,
                 likely_files, claims, parent_task_id, created_at, updated_at)
            VALUES
                (:id, :prd_id, :feature_id, :title, :description, :status, :priority,
                 :task_type, :dependencies, :conflict_groups, :scores,
                 :acceptance_criteria, :implementation_notes, :verification,
                 :likely_files, :claims, :parent_task_id, :created_at, :updated_at)
            ON CONFLICT(id) DO UPDATE SET
                prd_id               = excluded.prd_id,
                feature_id           = excluded.feature_id,
                title                = excluded.title,
                description          = excluded.description,
                -- status is intentionally OMITTED from the upsert. Status
                -- transitions go exclusively through task.status_changed
                -- events. If task.created carried status=proposed and we
                -- overwrote it on re-plan, a re-plan after Phase 4 claims
                -- would silently reset claimed/in_progress tasks back to
                -- proposed, stripping the claim. Greptile flagged this on
                -- PR #38; the fix is to let status be managed by its
                -- dedicated transition handler only.
                priority             = excluded.priority,
                task_type            = excluded.task_type,
                dependencies         = excluded.dependencies,
                conflict_groups      = excluded.conflict_groups,
                scores               = excluded.scores,
                acceptance_criteria  = excluded.acceptance_criteria,
                implementation_notes = excluded.implementation_notes,
                verification         = excluded.verification,
                likely_files         = excluded.likely_files,
                claims               = excluded.claims,
                parent_task_id       = excluded.parent_task_id,
                updated_at           = excluded.updated_at
            """,
            {
                "id": data["id"],
                "prd_id": prd_id,
                "feature_id": data["feature_id"],
                "title": data["title"],
                "description": data["description"],
                "status": data["status"],
                "priority": data["priority"],
                "task_type": data["task_type"],
                "dependencies": json.dumps(data["dependencies"]),
                "conflict_groups": json.dumps(data["conflict_groups"]),
                "scores": json.dumps(data["scores"]),
                "acceptance_criteria": json.dumps(data["acceptance_criteria"]),
                "implementation_notes": json.dumps(data["implementation_notes"]),
                "verification": json.dumps(data["verification"]),
                "likely_files": json.dumps(data["likely_files"]),
                "claims": json.dumps(data.get("claims") or []),
                "parent_task_id": data["parent_task_id"],
                "created_at": data["created_at"],
                "updated_at": data["updated_at"],
            },
        )

    def _insert_event_row(
        self,
        conn: sqlite3.Connection,
        event: Event,
        *,
        seq: int | None = None,
    ) -> None:
        """Insert the event into the events mirror table.

        ``seq`` is the replay-assigned display order — DERIVED state for git
        mode (where hash ids carry no order), never written back to the log.
        Local mode passes None and the column stays NULL because the
        monotonic id IS the order there.
        """
        data = event.model_dump(mode="json")
        conn.execute(
            """
            INSERT OR IGNORE INTO events
                (id, timestamp, actor, action, target_kind, target_id, payload_json, seq)
            VALUES
                (:id, :timestamp, :actor, :action, :target_kind, :target_id,
                 :payload_json, :seq)
            """,
            {
                "id": data["id"],
                "timestamp": data["timestamp"],
                "actor": data["actor"],
                "action": data["action"],
                "target_kind": data["target_kind"],
                "target_id": data["target_id"],
                "payload_json": json.dumps(data["payload_json"]),
                "seq": seq,
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers — error handling
    # ------------------------------------------------------------------

    def _safe_rollback(self, conn: sqlite3.Connection) -> None:
        """Attempt a ROLLBACK; ignore errors (connection may already be closed)."""
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass

    def _write_warn_to_audit(
        self,
        action: str,
        target_id: str,
        reason: str,
    ) -> None:
        """Write an idempotent-no-op warning to audit.jsonl.

        Used by ``_write_*`` methods that have post-mutation audit side-effects
        (e.g. ``_write_evidence_submitted``'s conditional claim auto-release warn).
        Unlike ``_append_audit_line`` this does not require an ``EventDraft``
        object — the action + target_id are sufficient.

        Writes to ``audit.jsonl`` (sibling of ``events.jsonl``); never to
        ``events.jsonl``.
        """
        audit_path = self._audit_path()
        now = self._clock.now().isoformat()
        record = {
            "ts": now,
            "kind": "idempotent_no_op",
            "action": action,
            "target_id": target_id,
            "reason": reason,
        }
        try:
            with open(audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Internal helpers — row → model conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(
        row: sqlite3.Row | tuple[Any, ...],
        conn: sqlite3.Connection,
    ) -> dict[str, Any]:
        """Convert a sqlite3 row (with description) to a plain dict."""
        # sqlite3.Row supports keys() if the connection has row_factory set.
        # We use description-based conversion to avoid requiring row_factory.
        if isinstance(row, sqlite3.Row):
            return dict(row)
        # Fallback: use the cursor description from a previous query.
        # This path should not be reached if callers use fetchone()/fetchall()
        # on a cursor configured with row_factory.
        raise RuntimeError(  # pragma: no cover
            "Unexpected row type; configure row_factory on the connection."
        )

    def _row_to_task(
        self,
        row: Any,
        conn: sqlite3.Connection,  # noqa: ARG002 — reserved for future join queries
    ) -> Task:
        """Deserialise a tasks row into a Task model instance."""
        d = dict(row)
        # JSON columns need parsing back.
        for col in (
            "dependencies",
            "conflict_groups",
            "acceptance_criteria",
            "implementation_notes",
            "likely_files",
            "claims",
        ):
            if isinstance(d.get(col), str):
                d[col] = json.loads(d[col])
        # Pre-v9 rows read through an un-migrated SELECT have no claims key;
        # NULL (pre-backfill) means "no claims". Drop so the default applies.
        if d.get("claims") is None:
            d.pop("claims", None)
        for col in ("scores", "verification"):
            if isinstance(d.get(col), str):
                d[col] = json.loads(d[col])
        return Task.model_validate(d)

    @staticmethod
    def _row_to_bundle(row: Any, conn: sqlite3.Connection) -> ExecutionBundle:
        """Deserialise a bundle row plus normalized, position-ordered members."""
        d = dict(row)
        member_rows = conn.execute(
            "SELECT task_id FROM execution_bundle_members "
            "WHERE bundle_id = ? ORDER BY position",
            (d["id"],),
        ).fetchall()
        d["task_ids"] = [member[0] for member in member_rows]
        for column in (
            "review_policy",
            "throughput_budget",
            "delegated_agents",
            "checkpoint",
        ):
            if isinstance(d.get(column), str):
                d[column] = json.loads(d[column])
        return ExecutionBundle.model_validate(d)

    @staticmethod
    def _row_to_bundle_claim(row: Any) -> BundleClaim:
        d = dict(row)
        for column in ("expected_files", "member_claim_ids"):
            if isinstance(d.get(column), str):
                d[column] = json.loads(d[column])
        return BundleClaim.model_validate(d)

    def _row_to_claim(self, row: Any) -> Claim:
        """Deserialise a claims row into a Claim model instance."""
        d = dict(row)
        if isinstance(d.get("expected_files"), str):
            d["expected_files"] = json.loads(d["expected_files"])
        return Claim.model_validate(d)

    @staticmethod
    def _row_to_review(row: Any) -> Review:
        """Deserialise a reviews row into a Review model instance.

        The reviews table stores two decision vocabularies:
        - prd.approved writes ``"approve"`` (ReviewDecision canonical value).
        - task.applied writes the raw outcome string (``"accepted"`` or
          ``"rejected"``), which predates the ReviewDecision enum.

        To allow the Review model's enum to validate correctly we map
        task-outcome values to their ReviewDecision equivalents using the
        module-level ``_TASK_OUTCOME_TO_REVIEW_DECISION`` constant:
          ``"accepted"`` → ``ReviewDecision.approve``   (``"approve"``)
          ``"rejected"`` → ``ReviewDecision.needs_changes`` (``"needs_changes"``)

        ``"rejected"`` maps to ``needs_changes`` (NOT ``reject``) because a
        rejected task auto-promotes to ``drafted`` for rework; it is not a
        terminal closure.  See _TASK_OUTCOME_TO_REVIEW_DECISION and
        _handle_task_applied for the full rationale.

        All other decision values (``"approve"``, ``"reject"``,
        ``"needs_changes"``) are passed through unchanged.
        """
        d = dict(row)
        raw_decision = d.get("decision")
        if raw_decision in _TASK_OUTCOME_TO_REVIEW_DECISION:
            d["decision"] = _TASK_OUTCOME_TO_REVIEW_DECISION[raw_decision]
        elif raw_decision is not None and raw_decision not in {v.value for v in ReviewDecision}:
            _valid = sorted(_TASK_OUTCOME_TO_REVIEW_DECISION) + [v.value for v in ReviewDecision]
            raise ValueError(
                f"_row_to_review: unexpected decision value {raw_decision!r}. "
                f"Expected one of {_valid}."
            )
        # A NULL decision column (raw_decision is None) is left as-is for
        # Review.model_validate to reject with a schema-level error, rather than
        # the misleading "unexpected value" mapping error above.
        return Review.model_validate(d)

    @staticmethod
    def _row_to_evidence(row: Any) -> Evidence:
        """Deserialise an evidence row (positional tuple) into an Evidence model instance.

        Row column order must match the SELECT used in list_evidence and
        get_latest_evidence:
          0:id  1:task_id  2:claim_id  3:commands_run  4:output_excerpt
          5:files_changed  6:pr_url  7:commit_sha  8:screenshots
          9:known_limitations  10:submitted_at  11:submitted_by  12:proofs
        ``proofs`` is index 12 (appended last) so the pre-v6 indices are stable;
        ``len(row) <= 12`` tolerates a row read before the v6 column landed.
        """
        import datetime

        from pydantic import TypeAdapter

        from anvil.state.models import Evidence as _Evidence
        from anvil.state.models import ProofArtifact as _ProofArtifact

        submitted_at = datetime.datetime.fromisoformat(row[10])
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=datetime.UTC)
        proofs_raw = row[12] if len(row) > 12 else None
        proofs = TypeAdapter(list[_ProofArtifact]).validate_json(proofs_raw or "[]")
        # v9 evidence-contracts category is index 13 (appended last so the
        # pre-v9 indices stay stable); tolerate short rows like proofs does.
        category = row[13] if len(row) > 13 and row[13] else "completion"
        return _Evidence(
            id=row[0],
            task_id=row[1],
            claim_id=row[2],
            commands_run=json.loads(row[3] or "[]"),
            output_excerpt=row[4],
            files_changed=json.loads(row[5] or "[]"),
            pr_url=row[6],
            commit_sha=row[7],
            screenshots=json.loads(row[8] or "[]"),
            known_limitations=row[9],
            proofs=proofs,
            category=category,
            submitted_at=submitted_at,
            submitted_by=row[11],
        )

    def _row_to_prd(self, row: Any) -> PRD:
        """Deserialise a prds row into a PRD model instance.

        Maps the v7 identity/release columns (id / title / target_version /
        target_tag / is_default / created_at / updated_at) into the PRD model.
        Those model fields carry ``Field(exclude=True)`` — they round-trip as
        in-memory attributes but stay out of ``model_dump()`` (and therefore out
        of ``serialize_state``), so reading them here is purely additive and the
        replay-equivalence golden is unaffected.
        """
        d = dict(row)
        # project_id is the migration's stored owner column. The PRD model has no
        # such field, so drop it from the validation dict (it is NOT exclude=True —
        # it simply does not exist on the model).
        d.pop("project_id", None)
        for col in (
            "goals",
            "non_goals",
            "requirements",
            "acceptance_criteria",
            "risks",
            "open_questions",
            "assumptions",
        ):
            if isinstance(d.get(col), str):
                d[col] = json.loads(d[col])
        # is_default is stored as INTEGER (0/1); coerce to bool for the model.
        if "is_default" in d and d["is_default"] is not None:
            d["is_default"] = bool(d["is_default"])
        # Review #13: created_at / updated_at are backfilled by the v6->v7
        # migration via COALESCE(last_reviewed_at, project.created_at), both of
        # which are stored as tz-aware UTC ISO strings — so the PRD field
        # validators (which reject naive datetimes) normally pass. Defensively
        # normalize any value that parses naive to UTC here, so get_prd() on a
        # migrated DB can never raise on a legacy row that predates tz
        # enforcement.
        for col in ("created_at", "updated_at", "last_reviewed_at"):
            d[col] = self._coerce_utc_iso(d.get(col))
        return PRD.model_validate(d)

    @staticmethod
    def _coerce_utc_iso(value: Any) -> Any:
        """Return an ISO timestamp string normalized to tz-aware UTC.

        A naive ISO string (no offset) is reinterpreted as UTC by appending the
        ``+00:00`` offset, so the PRD model's UTC field-validators accept it.
        Non-string / None / already-offset-aware values pass through unchanged.
        """
        if not isinstance(value, str) or not value:
            return value
        import datetime as _dt

        try:
            parsed = _dt.datetime.fromisoformat(value)
        except ValueError:
            return value
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=_dt.UTC).isoformat()
        return value

    def _row_to_project(self, row: Any) -> Project:
        """Deserialise a projects row into a Project model instance."""
        return Project.model_validate(dict(row))

    @staticmethod
    def _row_to_requirement(row: Any) -> Requirement:
        """Deserialise a requirements row into a Requirement model instance.

        Row column order must match the SELECT used in list_requirements:
          0:id  1:prd_section  2:text  3:source_paragraph  4:derived
          5:revision_introduced  6:revision_superseded  7:prd_id

        The ``derived`` column is stored as an integer (0/1) — bool() is
        applied so the Requirement model receives a proper Python bool.

        T023 — the lineage columns are surfaced so a revised PRD's requirements
        carry their correct introduction/supersession revisions in memory.
        ``revision_introduced`` is nullable for pre-lineage (v6-migrated) rows;
        NULL means "introduced at revision 1", so it falls back to the model
        default of 1 (the field is ``ge=1`` and would reject ``None``).
        ``revision_superseded`` stays nullable (None = still live).

        T024 — the ``prd_id`` partition column is surfaced so a multi-PRD DB's
        requirements carry their owning PRD in memory (the model field is
        ``exclude=True`` so model_dump() still drops it, but serialize_state
        reads ``rq.prd_id`` directly to partition the snapshot). A pre-v7 /
        single-PRD row carries ``DEFAULT_PRD_ID`` via the column DEFAULT, so this
        falls back to the model default of ``DEFAULT_PRD_ID`` when NULL.
        """
        revision_introduced = row[5] if row[5] is not None else 1
        return Requirement(
            id=row[0],
            prd_id=row[7] if row[7] is not None else DEFAULT_PRD_ID,
            prd_section=row[1],
            text=row[2],
            source_paragraph=row[3],
            derived=bool(row[4]),
            revision_introduced=revision_introduced,
            revision_superseded=row[6],
        )

    @staticmethod
    def _row_to_sync_mapping(row: Any) -> SyncMapping:
        """Deserialise a sync_mappings row into a SyncMapping model instance.

        The DB column ``provider_metadata_json`` is renamed to the model
        field ``provider_metadata`` after a JSON parse; ``external_url``
        passes through directly. The v7 partition columns ``prd_id`` /
        ``entity_kind`` map straight onto the (exclude=True) model fields, so a
        task-kind row surfaces its owning PRD without changing the serialized
        (``model_dump``) shape. Missing columns (older rows) default cleanly via
        the model's own defaults.
        """
        d = dict(row)
        raw_meta = d.pop("provider_metadata_json", None)
        if raw_meta:
            d["provider_metadata"] = json.loads(raw_meta)
        else:
            d["provider_metadata"] = {}
        # entity_kind is NOT NULL DEFAULT 'task' in the DDL, but guard a legacy
        # row that somehow stored NULL so model_validate falls back to the model
        # default instead of raising on a None Literal.
        if d.get("entity_kind") is None:
            d.pop("entity_kind", None)
        return SyncMapping.model_validate(d)
