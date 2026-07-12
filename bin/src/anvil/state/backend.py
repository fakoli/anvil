"""Storage backend protocol for anvil.

Defines the Backend Protocol that all storage implementations must satisfy,
plus the exception hierarchy for backend failures.

The SQLite implementation lives in state/sqlite.py (Wave 2).
This file ships the contract only — no I/O, no imports of concrete modules.

SL1-RR-1 (write-path rework): The ``append(EventDraft) -> Event | None``
method replaces the retired ``apply_event`` entry point. ``EventDraft`` carries
the intended mutation without an assigned id; the backend validates, assigns the
next monotonic id from the log, appends log-first, then applies the mutation.
``PENDING_EVENT_ID`` sentinel and ``apply_event`` / ``next_event_id`` are
fully removed (Task 6 migration complete).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from anvil.state.models import (
        PRD,
        BundleClaim,
        BundleReviewVerdict,
        Claim,
        ConflictGroup,
        Event,
        EventDraft,
        Evidence,
        ExecutionBundle,
        Feature,
        Project,
        Requirement,
        Review,
        SyncMapping,
        Task,
    )


class Backend(Protocol):
    """Storage backend interface. SQLite is the v0 impl; in-memory + Postgres are future.

    All mutating methods are expected to be atomic: the event is appended to the
    JSONL audit log and the SQLite state is updated in a single transaction.
    Replay from the event log must reproduce the exact same SQLite state.

    SL1-RR-1 write-path: ``append(EventDraft)`` is the sole production write
    entry point. The log is the id authority; ids are assigned from an in-memory
    counter seeded from the log's max id on open, so ``next_event_id()`` is
    removed from this protocol.
    """

    def initialize(self) -> None:
        """Open the backend, create schema if not present, validate version.

        Idempotent — safe to call repeatedly. Paths (db_path, events_path) are
        supplied to the concrete backend via its constructor; this method takes
        no arguments so impls with different connection models (in-memory,
        connection pool, etc.) can satisfy the Protocol without a path API.

        On open, if the events table is behind the log (log-ahead skew from a
        previous crash), the missing tail is re-applied via ``_write_*`` (forward
        catch-up) and the in-memory counter is seeded from the log max.

        Raises:
            SchemaMismatch: If an existing DB has a user_version that does not
                match the version this code expects.
        """
        ...

    def append(self, draft: EventDraft) -> Event | None:
        """Validate, assign id, log-first, then apply mutation. Sole production write.

        Steps (all inside the flock critical section):
          1. ``_check_<action>`` — validation only; raises ``EventRejected`` on
             an illegal transition / bad payload, or ``IdempotentNoOp`` when the
             request is legal but already satisfied.
          2. ``id = _next_seq()`` — increments the in-memory log-authority counter.
          3. Append the materialized ``Event`` line to ``events.jsonl`` (log-first).
          4. ``BEGIN IMMEDIATE; _write_<action>; _insert_event_row; COMMIT``.

        Returns:
            The materialized ``Event`` (with assigned id) on success.
            ``None`` for a legal idempotent no-op (audited in ``audit.jsonl``).

        Raises:
            EventRejected: Illegal transition or bad payload. Nothing written to
                ``events.jsonl``; one ``rejection`` line written to ``audit.jsonl``.
            TransactionAborted: Unexpected infrastructure failure *after* the log
                append (a ``_write_*`` or SQLite raise despite a passing check).
                The log line remains (append-only); SQLite is rolled back; a
                ``write_failed_after_log`` line is written to ``audit.jsonl``.
                Forward catch-up heals the skew on the next ``initialize()``.
            StateLocked: ``flock`` contention exceeded the timeout.
        """
        ...

    def get_task(self, task_id: str) -> Task | None:
        """Return the Task with the given ID, or None if not found."""
        ...

    def list_tasks(
        self,
        *,
        status: str | None = None,
        feature_id: str | None = None,
        task_type: str | None = None,
        prd_id: str | None = None,
    ) -> list[Task]:
        """Return tasks, optionally filtered by status, feature, task_type, prd_id.

        ``prd_id`` (T009) is the multi-PRD partition filter: ``None`` means all
        PRDs (unchanged), an explicit id scopes to ``prd_id = ?``.
        """
        ...

    def get_claim(self, claim_id: str) -> Claim | None:
        """Return the Claim with the given ID, or None if not found."""
        ...

    def get_bundle(self, bundle_id: str) -> ExecutionBundle | None:
        """Return the execution bundle with the given ID, or None."""
        ...

    def list_bundles(
        self, *, prd_id: str | None = None, status: str | None = None
    ) -> list[ExecutionBundle]:
        """Return execution bundles in stable ID order, optionally filtered."""
        ...

    def get_bundle_claim(self, bundle_id: str) -> BundleClaim | None:
        """Return the public coordinator claim for a bundle, if present."""
        ...

    def list_bundle_claims(self, *, status: str | None = None) -> list[BundleClaim]:
        """Return coordinator bundle claims in stable ID order."""
        ...

    def list_bundle_reviews(self, bundle_id: str) -> list[BundleReviewVerdict]:
        """Return adversarial verdicts for a bundle in deterministic order."""
        ...

    def get_feature(self, feature_id: str) -> Feature | None:
        """Return the Feature with the given ID, or None if not found."""
        ...

    def list_features(self, *, prd_id: str | None = None) -> list[Feature]:
        """Return Feature rows. Used by `plan` for orphan detection
        on re-parse (v1.15.0): the new parse's feature set is diffed
        against this to compute which features to delete.

        ``prd_id`` (T009): ``None`` means all PRDs (unchanged), an explicit id
        scopes to ``prd_id = ?``."""
        ...

    def list_conflict_groups(self) -> list[ConflictGroup]:
        """Return all persisted ConflictGroup rows (CL-4). Populated by
        `conflict_group.upserted` events emitted during planning/inference;
        surfaced by `anvil conflicts`."""
        ...

    def get_latest_evidence(self, task_id: str) -> Evidence | None:
        """Return the most recently submitted Evidence for task_id, or None.
        Used by `apply` to display the evidence summary."""
        ...

    def list_active_claims(self) -> list[Claim]:
        """Return all claims with a non-expired lease and non-terminal status."""
        ...

    def list_claims(self) -> list[Claim]:
        """Return ALL claims regardless of status, sorted by id ASC.

        Used by serialize_state and other snapshot paths that need the full
        claim history — not just the currently active subset returned by
        list_active_claims().  Includes active, released, stale, and
        force_released claims.
        """
        ...

    def list_reviews(self) -> list[Review]:
        """Return all Review rows sorted by id ASC.

        Used by serialize_state to capture the full review history.
        Includes approval reviews inserted by prd.approved as well as task
        reviews inserted by task.applied.
        """
        ...

    def list_task_review_decisions(self) -> list[tuple[str, str, str]]:
        """(task_id, decision, created_at_iso) for task.applied accepted/rejected
        outcomes, most-recent first. Backs the B49 accept-rate governor."""
        ...

    def list_evidence(self) -> list[Evidence]:
        """Return all Evidence rows sorted by id ASC.

        Used by serialize_state to capture every evidence submission for
        every task.  The ordering by id (E%06d-style primary key) guarantees
        deterministic output across runs.
        """
        ...

    def list_requirements(
        self, *, prd_id: str | None = None, include_superseded: bool = False
    ) -> list[Requirement]:
        """Return Requirement rows sorted by id ASC.

        The id-based ordering is deterministic because requirement IDs are
        assigned at parse time and never mutate.

        T023 — by default returns only the LIVE set (``revision_superseded IS
        NULL``): ``prd.revised`` is non-destructive (a superseded requirement
        keeps its row for lineage), so superseded rows are filtered out. A
        single-parse PRD has every row live, so this is unchanged for the
        pre-revision shape.

        ``include_superseded=True`` returns the full lineage (live + superseded)
        — used by serialize_state so the replay-equivalence oracle compares
        superseded-row lineage, not just the live set.

        ``prd_id`` (T009): ``None`` means all PRDs (unchanged), an explicit id
        scopes to ``AND prd_id = ?``.
        """
        ...

    def list_events(
        self,
        *,
        target_id: str,
        target_kind: str | None = None,
        limit: int = 10,
    ) -> list[tuple[str, str]]:
        """Return recent events for the given target as (action, timestamp_iso) tuples,
        most-recent first. Used by `show` to surface task history."""
        ...

    def latest_event_payload(
        self, target_id: str, action: str
    ) -> tuple[dict, str] | None:
        """Return the most recent *action* event's (payload, timestamp_iso)
        for ``target_id``, or None. retro-opps T012 — the heartbeat-bus
        read-back seam (``list_events`` drops payloads)."""
        ...

    def first_event_id(self, target_id: str) -> str | None:
        """Return the id of the earliest-recorded event for ``target_id``, or
        None if it has none. Used to bind an ``AcceptanceProof`` to the
        event-log range it covers (B48 part 2)."""
        ...

    def get_prd(self, prd_id: str | None = None) -> PRD | None:
        """Return a PRD, or None if not found.

        ``prd_id is None`` resolves the ``is_default = 1`` row — the single PRD
        on a single-PRD DB (the legacy no-arg call shape). An explicit ``prd_id``
        resolves ``WHERE id = ?`` (the T008 multi-PRD partition lookup).
        """
        ...

    def list_prds(self) -> list[PRD]:
        """Return every PRD ordered by ``id`` ASC (T008)."""
        ...

    def default_prd_id(self) -> str | None:
        """Return the ``is_default = 1`` PRD's id, or None if no PRD exists (T008)."""
        ...

    def get_prd_for_task(self, task: Task) -> PRD | None:
        """Return the PRD that OWNS ``task``, resolved via ``task.prd_id`` (T011).

        Reads ``task.prd_id`` directly — no ``Feature`` join — then ``get_prd``
        on that id. Falls back to the default PRD (``get_prd()`` /
        ``is_default = 1``) when the task carries no ``prd_id``. The per-PRD
        claim gate keys on this, so a task in an approved PRD is claimable while
        a sibling in a draft PRD is refused at ``prd_status_gate``.
        """
        ...

    def get_project(self) -> Project | None:
        """Return the Project record, or None if not initialised."""
        ...

    def replay_from_empty(self, events_path: str) -> None:
        """Reconstruct state.db from events.jsonl. The audit-guarantee primitive.

        Drops and recreates all tables, then replays every event in order via
        ``_write_*`` only — no validation, no logging, no skip-list. Every line
        in ``events.jsonl`` is a fact; an interior malformed line raises. Only a
        torn trailing line (from a crash mid-append) is tolerated. After replay
        the DB is byte-for-byte equivalent to the state produced by the original
        sequence of ``append()`` calls.

        Args:
            events_path: Absolute path to the JSONL event log to replay.
        """
        ...

    def replay_to_event_id(self, events_path: str, stop_after_event_id: str) -> None:
        """Reconstruct state.db AS OF a bounded prefix of the log (read-only).

        Like :meth:`replay_from_empty`, but stops after applying the event whose
        id equals ``stop_after_event_id`` — every event ordered after it is left
        unapplied, rebuilding the projection exactly as it stood when that event
        committed. Reuses the same apply path and per-mode (local/git) ordering
        and torn-trailing-line tolerance as ``replay_from_empty``, so the rebuilt
        prefix is byte-identical to the prefix a full replay would produce.

        Read-only: writes only the projection, never appends to ``events.jsonl``.
        Raises ``ValueError`` if ``stop_after_event_id`` is absent from the log.

        Args:
            events_path: Absolute path to the JSONL event log to replay.
            stop_after_event_id: The last event id to apply; events after it in
                canonical order are skipped.
        """
        ...

    def close(self) -> None:
        """Release any held resources (connections, file handles)."""
        ...

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
        mapping wins is ASC-sort-position-dependent and brittle. Callers
        that need to enumerate every mapping for a task should use
        ``list_sync_mappings`` and filter by ``task_id`` instead.
        """
        raise NotImplementedError(
            "Backend.get_sync_mapping() must be implemented by the concrete backend."
        )

    def list_sync_mappings(
        self,
        external_system: str | None = None,
    ) -> list[SyncMapping]:
        """Return SyncMapping rows, optionally filtered by external_system."""
        raise NotImplementedError(
            "Backend.list_sync_mappings() must be implemented by the concrete backend."
        )

    def apply_sync_mapping(
        self,
        mapping: SyncMapping,
        *,
        actor: str = "system",
    ) -> Event:
        """Convenience wrapper that emits a sync_mapping.upserted event.

        Builds an ``EventDraft`` and calls ``append()``, returning the
        materialized ``Event`` (with the backend-assigned id). The underlying
        SQLite mutation is the upsert described by ``_write_sync_mapping_upserted``.
        ``actor`` is recorded in the event audit row; defaults to ``"system"``.
        """
        raise NotImplementedError(
            "Backend.apply_sync_mapping() must be implemented by the concrete backend."
        )


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class BackendError(Exception):
    """Base for backend failures (concurrency, schema mismatch, etc.)."""


class TransactionAborted(BackendError):
    """Unexpected infrastructure failure after the log append.

    Under the SL1-RR-1 write-path rework this means only a genuine infra
    failure (disk error, SQLite OperationalError, a bug in a ``_write_*``) that
    occurs *after* the event line has been appended to ``events.jsonl``. The log
    line stays (append-only); SQLite is rolled back; a ``write_failed_after_log``
    line is written to ``audit.jsonl``. Forward catch-up on the next
    ``initialize()`` heals the skew.

    Distinct from :class:`EventRejected` (validation failure before any log
    write) and :class:`StateLocked` (flock contention).
    """


class StateLocked(BackendError):
    """SQLite busy_timeout exceeded; another writer held the lock too long."""


class EventRejected(BackendError):
    """An ``append`` was refused before anything was logged.

    Raised by a ``_check_<action>`` on an illegal transition or a bad payload
    (e.g. claiming an already-claimed task, evidence missing required fields).
    This is a *normal, expected* control-flow signal on the write path — not an
    infrastructure failure. Nothing is written to ``events.jsonl``; the rejection
    is recorded in the sibling ``audit.jsonl`` and re-raised to the caller.

    Distinct from :class:`TransactionAborted`, which (under the SL1-RR-1
    write-path rework) is narrowed to mean only an unexpected infrastructure
    failure *after* the log append.
    """


class IdempotentNoOp(BackendError):
    """An ``append`` request that is legal but already satisfied — a no-op.

    Raised by a ``_check_<action>`` when the requested mutation has already
    happened (e.g. releasing an already-released claim). This is *not* an error:
    ``append`` catches it internally, records an ``idempotent_no_op`` line in
    ``audit.jsonl``, and returns ``None`` without logging a canonical event or
    mutating state. It is defined as an exception purely so a ``_check_*`` can
    signal this third outcome (alongside "proceed" and ``EventRejected``) via
    control flow; callers of ``append`` never see it.
    """


class SchemaMismatch(BackendError):
    """DB schema version != code expected version."""
