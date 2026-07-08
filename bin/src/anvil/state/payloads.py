"""Per-action Pydantic v2 payload models for _apply_mutation dispatch.

Each model corresponds to exactly one event action.  ``extra='forbid'`` is set
on every model so that unknown keys raise ``ValidationError`` at the point of
dispatch — before any handler body executes.  This centralises payload validation
that was previously scattered across 17 handler bodies as ad-hoc ``payload.get``
lookups.

Handlers receive the validated typed model instead of the raw ``dict[str, Any]``.
They access fields via attribute access (``payload.project_id``) rather than
``payload.get("project_id")``.

The ``Any`` type is kept for list-of-dict fields (e.g. ``requirements``,
``subtasks``) because those sub-dicts are themselves validated by Pydantic models
inside the handler bodies (``Requirement.model_validate``, ``Task.model_validate``).
Constraining them here would duplicate that validation.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from anvil.state.models import DEFAULT_PRD_ID, ProofArtifact


class ProjectCreatedPayload(BaseModel):
    """Payload for 'project.created' — maps directly to the Project model."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    created_at: str
    updated_at: str


class StateInitializedPayload(BaseModel):
    """Payload for 'state.initialized' — no required fields (audit-only seed event)."""

    model_config = ConfigDict(extra="forbid")


class PrdParsedPayload(BaseModel):
    """Payload for 'prd.parsed'.

    ``prd_id`` defaults to ``DEFAULT_PRD_ID`` ('default') so a PRE-v7
    ``prd.parsed`` event (which never carried a ``prd_id`` key) deserialises
    into the default PRD partition — preserving replay equivalence. The
    identity/release fields (title/target_version/target_tag/is_default) also
    default so old payloads validate unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    prd_id: str = DEFAULT_PRD_ID
    title: str = ""
    target_version: str | None = None
    target_tag: str | None = None
    is_default: bool = True
    status: str = "draft"
    summary: str = ""
    goals: list[Any] = []
    non_goals: list[Any] = []
    requirements: list[Any] = []
    acceptance_criteria: list[Any] = []
    risks: list[Any] = []
    open_questions: list[Any] = []


class PrdRevisedPayload(BaseModel):
    """Payload for 'prd.revised' (v0.3 multi-PRD, Phase 6).

    Emitted when an existing ``prd_id`` is re-parsed. Carries the new
    ``revision`` number, the scalar PRD fields (mirroring
    :class:`PrdParsedPayload` so the revised PRD row is fully described), and a
    self-describing requirement diff:

    - ``requirements_added`` — requirements new in this revision.
    - ``requirements_superseded`` — requirements removed/replaced by this
      revision (the handler marks ``revision_superseded`` on the prior rows).
    - ``requirements_unchanged`` — requirements carried forward verbatim.

    The diff is the source of truth for the status-demotion rule: a revision
    that supersedes/removes any requirement demotes an approved PRD back to
    ``draft``; a pure-additive revision (empty ``requirements_superseded``)
    keeps the current status.

    ``extra='forbid'`` rejects unknown keys at dispatch. The ``list[Any]``
    requirement-diff fields hold raw requirement dicts that the handler
    validates via ``Requirement.model_validate`` (mirroring
    :class:`PrdParsedPayload.requirements`).
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    prd_id: str = DEFAULT_PRD_ID
    revision: int = Field(ge=1)
    # Scalar PRD fields (mirror PrdParsedPayload) — describe the revised PRD row.
    title: str = ""
    target_version: str | None = None
    target_tag: str | None = None
    # Unlike PrdParsedPayload (where 'first parse of the default PRD' justifies
    # is_default=True), a 'revise' event always targets an EXISTING prd_id. For
    # any non-default named PRD that is is_default=False, so the safe default
    # here is False: a Phase 6 prd.revised handler that trusts this value must
    # not flip a named PRD's is_default 0→1 (that would create two is_default=1
    # rows, violating the ux_prds_default single-default invariant and the v7
    # identity preservation _write_prd_parsed protects). The default PRD's
    # revision carries is_default=True explicitly.
    is_default: bool = False
    status: str = "draft"
    summary: str = ""
    goals: list[Any] = []
    non_goals: list[Any] = []
    acceptance_criteria: list[Any] = []
    risks: list[Any] = []
    open_questions: list[Any] = []
    # Self-describing requirement diff (raw dicts; handler validates each).
    requirements_added: list[Any] = []
    requirements_superseded: list[Any] = []
    requirements_unchanged: list[Any] = []


class PrdReviewedPayload(BaseModel):
    """Payload for 'prd.reviewed'.

    ``prd_id`` defaults to ``DEFAULT_PRD_ID`` so pre-v7 events (which scoped the
    status change to the singleton via ``project_id`` only) replay into the
    default PRD partition.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    prd_id: str = DEFAULT_PRD_ID
    reviewer: str
    notes: str | None = None


class PrdApprovedPayload(BaseModel):
    """Payload for 'prd.approved'.

    ``prd_id`` defaults to ``DEFAULT_PRD_ID`` for pre-v7 replay equivalence
    (see :class:`PrdReviewedPayload`).
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    prd_id: str = DEFAULT_PRD_ID
    approver: str


class PrdDecisionResolvedPayload(BaseModel):
    """Payload for 'prd.decision_resolved' (T018 — decision back-propagation).

    Audit-only: the event row IS the record of a decision being answered and
    written back into ``prd.md``. No SQLite table is mutated — the PRD is
    refreshed by a later ``prd parse`` — so replay treats this as a no-op.
    The ``before``/``after`` fields capture the exact span the back-propagation
    rewrote, so the log is a self-describing audit trail of what changed.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    decision_id: str
    decision_kind: str
    prd_ref: str
    resolution: str
    resolved_by: str
    section: str = ""
    before: str = ""
    after: str = ""


class FeatureCreatedPayload(BaseModel):
    """Payload for 'feature.created' — forwarded to Feature.model_validate."""

    model_config = ConfigDict(extra="forbid")

    id: str
    prd_id: str = DEFAULT_PRD_ID
    title: str
    description: str = ""
    status: str = "proposed"
    requirements: list[Any] = []
    tasks: list[Any] = []


class TaskCreatedPayload(BaseModel):
    """Payload for 'task.created' — forwarded to Task.model_validate.

    Sub-field validation (scores, verification, etc.) is delegated to
    Task.model_validate inside the handler.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    feature_id: str
    prd_id: str = DEFAULT_PRD_ID
    title: str
    description: str = ""
    status: str = "proposed"
    priority: str = "medium"
    # T015: defaults to 'feature' so a payload that predates non-feature task
    # types (or any minimal hand-rolled caller) deserialises unchanged.
    task_type: str = "feature"
    dependencies: list[Any] = []
    conflict_groups: list[Any] = []
    scores: dict[str, Any] | None = None
    acceptance_criteria: list[Any] = []
    implementation_notes: list[Any] = []
    verification: dict[str, Any] | None = None
    likely_files: list[Any] = []
    # Evidence contracts (issue #153) — optional so pre-existing JSONL rows
    # (no ``claims`` key) replay unchanged; sub-validation is delegated to
    # Task.model_validate like scores/verification.
    claims: list[Any] | None = None
    parent_task_id: str | None = None
    created_at: str = ""
    updated_at: str = ""


class TaskScoredPayload(BaseModel):
    """Payload for 'task.scored'."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    scores: dict[str, Any]
    explanation: str | None = None


class TaskExpandedPayload(BaseModel):
    """Payload for 'task.expanded'."""

    model_config = ConfigDict(extra="forbid")

    parent_task_id: str
    subtasks: list[Any]


class TaskStatusChangedPayload(BaseModel):
    """Payload for 'task.status_changed'.

    The JSON keys ``from`` and ``to`` are Python keywords so they are mapped
    via ``Field(alias=...)`` to ``from_status`` and ``to_status``.
    ``populate_by_name=True`` lets callers also use the Python names directly
    (useful in tests).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    task_id: str
    from_status: str = Field(alias="from")
    to_status: str = Field(alias="to")
    reason: str | None = None


class TaskDeletedPayload(BaseModel):
    """Payload for 'task.deleted' — orphan cleanup on re-parse (v1.15.0).

    Emitted by ``anvil plan`` when a task that existed in state.db
    is no longer present in the re-parsed PRD. The handler in sqlite.py
    deletes the task row + related subtask/dependency entries.

    Safety: by default the handler refuses to delete a task in a non-safe
    status (claimed / in_progress / needs_review / accepted / rejected /
    done / blocked) — those carry claim or evidence history that should
    not silently vanish. The ``force`` flag bypasses the check and is
    the explicit mechanism behind the CLI's ``--prune-force`` flag.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    force: bool = False
    reason: str = ""


class FeatureDeletedPayload(BaseModel):
    """Payload for 'feature.deleted' — orphan cleanup on re-parse (v1.15.0).

    Emitted alongside ``task.deleted`` events when a feature is removed
    from the PRD. The handler refuses to delete a feature that still has
    referencing tasks in state.db — task deletions must land first.
    The schema's ``tasks.feature_id ... ON DELETE RESTRICT`` foreign key
    enforces the same guarantee at the SQL layer as a belt-and-braces
    backstop.
    """

    model_config = ConfigDict(extra="forbid")

    feature_id: str
    force: bool = False
    reason: str = ""


class ConflictGroupUpsertedPayload(BaseModel):
    """Payload for 'conflict_group.upserted' (CL-4).

    Persists a ConflictGroup computed during planning/inference into the
    ``conflict_groups`` table so the file-overlap groupings round-trip across
    process boundaries. Forwarded to ``ConflictGroup.model_validate`` in the
    handler.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    task_ids: list[Any] = []
    reason: str = ""


class ClaimCreatedPayload(BaseModel):
    """Payload for 'claim.created'."""

    model_config = ConfigDict(extra="forbid")

    id: str
    task_id: str
    claimed_by: str
    claim_type: str
    status: str
    created_at: str
    lease_expires_at: str
    last_heartbeat_at: str
    branch: str | None = None
    worktree_path: str | None = None
    expected_files: list[Any] = []
    # Optional terminal-state fields — present when reading back a Claim
    # model that has already been released/staled (e.g. in replay scenarios
    # where the full Claim dict is passed as the event payload).
    released_at: str | None = None
    release_reason: str | None = None
    # TOCTOU fix — whether this claim was made with --force. Threaded through
    # so the in-transaction expected_files overlap re-check (the source of
    # truth, run inside BEGIN IMMEDIATE) can be overridden exactly like the
    # pre-claim CLI check. Defaults to False so events written before this
    # field existed (and replayed events) still validate and behave as a
    # non-forced claim.
    force: bool = False


class ClaimReleasedPayload(BaseModel):
    """Payload for 'claim.released'."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    released_by: str
    release_reason: str | None = None
    force: bool = False
    # Audit timestamp emitted by ClaimManager.release()
    released_at: str | None = None


class ClaimRenewedPayload(BaseModel):
    """Payload for 'claim.renewed'."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    lease_expires_at: str
    last_heartbeat_at: str
    # Audit field emitted by ClaimManager.renew()
    renewed_by: str | None = None


class ClaimStalePayload(BaseModel):
    """Payload for 'claim.stale'.

    All fields sent by stale.py (the stale detector) are listed here.
    """

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    detected_at: str
    reason: str
    # Additional fields sent by the stale detector (stale.py)
    task_id: str | None = None
    expired_at: str | None = None
    actor: str | None = None


class EvidenceSubmittedPayload(BaseModel):
    """Payload for 'evidence.submitted'."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    claim_id: str
    submitted_by: str
    evidence_id: str
    commands_run: list[Any] = []
    files_changed: list[Any] = []
    output_excerpt: str | None = None
    pr_url: str | None = None
    commit_sha: str | None = None
    screenshots: list[Any] = []
    known_limitations: str | None = None
    # SL-3 / B48: typed proofs, validated at write time (a malformed proof
    # raises here, not silently at replay). Defaults to [] so pre-SL-3 logs
    # (no ``proofs`` key) stay replayable.
    proofs: list[ProofArtifact] = Field(default_factory=list)
    # Evidence contracts (issue #153) — optional evidence role; absent key
    # (every pre-existing row) means completion, the historical meaning.
    category: str | None = None


class TaskAppliedPayload(BaseModel):
    """Payload for 'task.applied'."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    reviewer: str
    decision: str
    notes: str | None = None


class FileChangedPayload(BaseModel):
    """Payload for 'file_changed' — audit-trail-only event.

    The handler is a no-op.  This model exists to make the dispatch table
    uniform and to validate that the payload shape is known.

    Known fields come from both the CLI hook (hooks.py) and the bash hook
    (hooks/record-file-change.sh).  All fields are optional because older
    JSONL records may omit some of them.
    """

    model_config = ConfigDict(extra="forbid")

    file: str | None = None
    tool: str | None = None
    actor: str | None = None
    changed_at: str | None = None
    # Bash hook uses these keys instead of the CLI hook convention:
    entity_type: str | None = None
    entity_id: str | None = None
    source: str | None = None


class ProgressNotedPayload(BaseModel):
    """Payload for 'progress.noted' — audit-trail-only event emitted by the MCP
    submit_progress tool (Phase 6).

    The handler is a no-op: the JSONL row is the audit record.  No SQLite
    mutation is performed — task status does not change.  This model exists to
    keep the dispatch table uniform and to enforce the known field schema via
    extra='forbid'.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    actor: str
    notes: str
    noted_at: str
    # retro-opps T010 — optional structured phase for the heartbeat bus.
    # Optional so every pre-T010 JSONL row still replays; extra='forbid'
    # still rejects unknown keys.
    phase: str | None = None
    detail: str | None = None


class SyncMappingUpsertedPayload(BaseModel):
    """Payload for 'sync_mapping.upserted' — Phase 8 external-system sync.

    Mirrors the SyncMapping model (state/models.py). The handler INSERTs a new
    row or, on (task_id, external_system) conflict, UPDATEs the existing row.

    Field-by-field mirror of :class:`anvil.state.models.SyncMapping`.
    When a new field is added to ``SyncMapping`` it MUST be added here too —
    ``apply_sync_mapping`` constructs this payload from the canonical model
    and any mismatch surfaces at the call site (extra='forbid' on this model).
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str | None = None
    external_system: str
    external_id: str
    external_url: str | None = None
    last_synced_at: str
    sync_state: str = "in_sync"
    conflict_resolution_strategy: str = "prompt"
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    # v0.3 multi-PRD partition (T026). Both default so a PRE-change
    # ``sync_mapping.upserted`` event (which never carried these keys)
    # deserialises with ``entity_kind='task'`` and ``prd_id='default'`` — the
    # same shape the in-place v6->v7 migration backfills (every legacy mapping
    # is owned by the default PRD), so replay reconstructs the migrated table.
    prd_id: str = DEFAULT_PRD_ID
    entity_kind: Literal["task", "prd"] = "task"


class SyncMappingDeletedPayload(BaseModel):
    """Payload for 'sync_mapping.deleted' — Phase 8 external-system sync.

    Composite-key delete: removes all sync_mappings rows for ``task_id``
    (across every external_system). If you need per-system deletion supply
    ``external_system`` as well; when None, every mapping for the task is
    removed.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    external_system: str | None = None


class TaskSyncedFromRemotePayload(BaseModel):
    """Payload for 'task.synced_from_remote' — Phase 8 pull-applies-remote.

    Emitted by the sync CLI's pull path when the remote payload has
    legitimately moved ahead of local state (``remote_moved AND NOT
    local_moved``) — i.e. a non-conflict update. The handler overwrites
    the local Task's ``title``, ``description``, and ``status`` fields
    with the remote values, then bumps ``updated_at``.

    Why a dedicated action (rather than re-using ``task.status_changed``
    or ``task.created``)?
    * ``task.status_changed`` only carries status, so title/description
      updates would silently drop.
    * ``task.created`` with INSERT OR REPLACE semantics would risk losing
      Task fields not present in the remote payload (scores, dependencies,
      verification, …).

    The forbid-extras schema means callers cannot accidentally smuggle
    other Task fields through the pull path — anything the remote knows
    must be explicitly added to this model first.

    Fields
    ------
    task_id:
        Local task id (``T001``) to mutate.
    title, description, status:
        New values pulled from the remote. ``status`` is the local
        :class:`anvil.state.models.TaskStatus` value (already
        translated by the provider, e.g. via ``LABEL_TO_STATUS``).
    actor:
        Audit string for who/what triggered this pull (e.g.
        ``"sync.github_issues"``).
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: str
    description: str
    status: str
    actor: str | None = None


# ---------------------------------------------------------------------------
# Phase 8 Wave 3 — sync.* audit events (Task 6 / cli/sync.py)
# Phase 9 T3 — discriminated union (one concrete subclass per `sync.*` action)
# ---------------------------------------------------------------------------
#
# Every push / pull / conflict / batch operation emits a `sync.*` action
# whose handler is an audit-only no-op (like ``file_changed`` and
# ``progress.noted``).  The JSONL row IS the audit record; no SQLite mutation
# is performed by these events themselves.  The actual mapping persistence
# happens via the existing ``sync_mapping.upserted`` event, kept separate so
# replay-from-empty can reconstruct mappings without depending on ``sync.*``.
#
# Phase 8 used a SINGLE all-optional ``SyncAuditPayload`` model — every field
# was ``str | None = None``.  That accepted ``strategy="foo"`` on a
# ``sync.batch.completed`` event without complaint, and missed the chance to
# enforce that ``sync.push.failed`` actually carries an
# ``exception_message``.  Phase 9 T3 replaces it with a Pydantic v2
# discriminated union: each action has its own subclass with only the fields
# that action actually carries, ``extra="forbid"`` rejects unknown keys, and
# the discriminator dispatches O(1) on the ``action`` literal.
#
# Backwards compatibility:
# * ``SyncAuditPayload`` is preserved as a module-level type-form —
#   ``Annotated[Union[...], Field(discriminator="action")]`` — so existing
#   imports still resolve.  This is NOT a ``typing.TypeAlias`` (no
#   ``: TypeAlias =`` annotation) and NOT a ``BaseModel`` subclass; it is a
#   Pydantic discriminated-union *type form*.  Callers that used to write
#   ``SyncAuditPayload.model_validate(d)`` directly MUST migrate to
#   ``TypeAdapter(SyncAuditPayload).validate_python(d)`` OR look up the
#   concrete subclass from ``ACTION_TO_PAYLOAD``.
# * ``ACTION_TO_PAYLOAD`` maps each ``sync.*`` action string to its concrete
#   subclass for direct dispatcher lookup
#   (``state/sqlite.py:_apply_mutation`` is the obvious consumer).
#
# Action coverage — the T3 plan enumerates 10 action strings (push.{started,
# completed, deferred, failed}, pull.{started, completed, deferred, failed},
# reconciliation.{started, completed}).  ``cli/sync.py`` ALSO emits
# ``sync.batch.{started, completed}`` and ``sync.conflict_detected`` today,
# and ``state/sqlite.py``'s dispatch table registers all of them against
# ``SyncAuditPayload``.  Omitting them from the union would break the
# dispatcher the moment T3 lands.  We therefore include subclasses for the
# emitted-today set as well; the T3-listed reconciliation events are
# included for forward-compat (no emitter today; T5+ may add them).


class _SyncAuditBase(BaseModel):
    """Private base for every ``sync.*`` audit payload.

    Carries the fields that EVERY ``sync.*`` event populates: the registry
    ``provider_id`` of the provider in play.  ``extra="forbid"`` is set here
    so every subclass inherits strict-mode validation without restating it.

    The ``action`` discriminator field is declared on each concrete subclass
    as a ``Literal[...]`` with a default value, so the union dispatches O(1)
    on ``action`` AND construction without the kwarg still works
    (``SyncPushStartedPayload(provider_id="github_issues")``).
    """

    model_config = ConfigDict(extra="forbid")

    # provider_id is the closest thing to a universally-present field.
    # ``sync.batch.*`` and ``sync.push.*`` / ``sync.pull.*`` all carry it.
    # The forward-compat reconciliation events leave it optional because a
    # bare reconciliation scan spans every provider.
    provider_id: str | None = None


class SyncBatchStartedPayload(_SyncAuditBase):
    """Payload for ``sync.batch.started`` — entry to ``_run_sync_once``.

    Emitted before any per-task work happens; pairs with a terminal
    ``sync.batch.completed``.
    """

    action: Literal["sync.batch.started"] = "sync.batch.started"
    direction: str | None = None  # "push" / "pull" / "both"
    audit_note: str | None = None  # e.g. "task=T001" when --task is scoped


class SyncBatchCompletedPayload(_SyncAuditBase):
    """Payload for ``sync.batch.completed`` — terminal of ``_run_sync_once``.

    The ``audit_note`` carries the per-iteration totals string
    (``"pushed=N pulled=M failed_push=… failed_pull=… manual_merge_pending=…"``)
    OR the literal ``"no tasks"`` when ``_select_tasks_for_sync`` returned
    empty.  Treat it as descriptive, not as a structured field.
    """

    action: Literal["sync.batch.completed"] = "sync.batch.completed"
    audit_note: str | None = None


class SyncPushStartedPayload(_SyncAuditBase):
    """Payload for ``sync.push.started`` — entry to ``_push_one_task``."""

    action: Literal["sync.push.started"] = "sync.push.started"
    task_id: str
    external_id: str | None = None  # None on first-push (no remote id yet)
    direction: Literal["push"] = "push"


class SyncPushCompletedPayload(_SyncAuditBase):
    """Payload for ``sync.push.completed`` — successful provider push + mapping upsert.

    ``external_id`` is required here (unlike on ``*.started``) because by
    definition the provider has returned an ``ExternalRef`` whose id we
    persisted into the SyncMapping.
    """

    action: Literal["sync.push.completed"] = "sync.push.completed"
    task_id: str
    external_id: str
    direction: Literal["push"] = "push"


class SyncPushDeferredPayload(_SyncAuditBase):
    """Payload for ``sync.push.deferred`` — push intent recorded but not executed.

    Reserved for the T5 ``local_moved``-only path (see Phase 9 T1/T5 plan).
    The local Task has moved ahead of the mapping's ``last_synced_at`` but
    no remote push has yet caught it up; the mapping is bumped to
    ``SyncState.local_ahead`` and this event records the deferral so
    operators monitoring the audit stream can tell ``deferred`` apart from
    ``completed`` or ``failed``.

    ``resolution`` carries a short machine-readable token describing WHY
    the push was deferred (e.g. ``"local_moved_no_push"``).  Free-form
    string at this layer — T5 will define the controlled vocabulary.
    """

    action: Literal["sync.push.deferred"] = "sync.push.deferred"
    task_id: str
    external_id: str | None = None
    direction: Literal["push"] = "push"
    resolution: str | None = None
    audit_note: str | None = None


class SyncPushFailedPayload(_SyncAuditBase):
    """Payload for ``sync.push.failed`` — ``provider.push_task(...)`` raised.

    ``exception_type`` and ``exception_message`` are required: the whole
    point of the ``failed`` event is to capture WHY the push failed.  A
    failed event without an exception message is a contract violation we
    want Pydantic to catch at validate time.
    """

    action: Literal["sync.push.failed"] = "sync.push.failed"
    task_id: str
    exception_type: str
    exception_message: str
    direction: Literal["push"] = "push"


class SyncPullStartedPayload(_SyncAuditBase):
    """Payload for ``sync.pull.started`` — entry to ``_pull_one_task`` after
    ``existing`` SyncMapping is resolved.

    ``external_id`` is required here because the pull is fetched by the
    remote id (``provider.fetch_task(external_id=existing.external_id)``);
    a pull with no remote id to fetch by would have early-returned as
    ``skipped`` before this event fires.
    """

    action: Literal["sync.pull.started"] = "sync.pull.started"
    task_id: str
    external_id: str
    direction: Literal["pull"] = "pull"


class SyncPullCompletedPayload(_SyncAuditBase):
    """Payload for ``sync.pull.completed`` — pull terminal when the pull
    itself was honest (fetch succeeded, mapping bumped to a truthful
    state), even if no local Task row was mutated this iteration.

    Phase 9 T5 splits the old over-broad terminal into two events:
    * ``sync.pull.completed`` — fires when:

      1. A clean pull mutated the local Task (``_apply_remote_to_local``
         ran), OR
      2. The mapping was flipped to ``SyncState.external_deleted``
         (tombstone branch — ``audit_note="external_deleted"``), OR
      3. No divergence existed and the mapping was bumped to
         ``SyncState.in_sync``, OR
      4. The ``local_moved``-only branch — fetch succeeded, no remote
         movement observed, mapping bumped to ``SyncState.local_ahead``.
         The push hint is what's deferred (a paired
         ``sync.push.deferred`` event with
         ``resolution="local_moved_no_push"`` fires), NOT the pull
         terminal itself.  T5 chose this over emitting
         ``sync.pull.deferred`` because the pull WAS honest — only the
         follow-up push needs operator attention.

    * ``sync.pull.deferred`` — fires when the conflict-resolution
      recorded an intent without mutating local state (see
      :class:`SyncPullDeferredPayload`).

    ``audit_note`` carries optional context — today the tombstone branch
    sets it to ``"external_deleted"``.  ``resolution`` is OPTIONAL here:
    a clean pull has no resolution; an immediate-apply conflict branch
    will set ``resolution="remote_wins_applied"`` (T5 deferred to a
    future phase per ``docs/plans/agent-welder-honesty-status.md``).
    """

    action: Literal["sync.pull.completed"] = "sync.pull.completed"
    task_id: str
    external_id: str
    direction: Literal["pull"] = "pull"
    audit_note: str | None = None
    resolution: str | None = None


class SyncPullDeferredPayload(_SyncAuditBase):
    """Payload for ``sync.pull.deferred`` — pull terminal when no local Task
    mutation happened this iteration.

    Two emission paths today (Phase 9 T1 audit):
    * ``manual_merge`` strategy — the merge file was written; the operator
      must act before the pull can complete.  ``audit_note`` is set to
      ``"manual_merge_pending"``.
    * (T5) the five deferred conflict-resolution branches (``local_wins``,
      ``remote_wins``, ``prompt_defaulted_to_local``, ``prompt_chose_local``,
      ``prompt_chose_remote``, plus the catch-all ``prompt_skipped``).
      ``resolution`` carries the branch identifier so the JSONL is
      self-describing.

    ``resolution`` is intentionally a free-form ``str`` and NOT a
    ``Literal[...]`` of known tokens at this layer.  The T5 work in
    ``cli/sync.py`` is what defines the controlled vocabulary
    (``local_wins`` vs ``local_wins_deferred`` is still being debated —
    see status file).  Pinning the literal set here would force a
    payloads.py edit every time T5 adds a new resolution branch.
    """

    action: Literal["sync.pull.deferred"] = "sync.pull.deferred"
    task_id: str
    external_id: str
    direction: Literal["pull"] = "pull"
    resolution: str | None = None
    audit_note: str | None = None


class SyncPullFailedPayload(_SyncAuditBase):
    """Payload for ``sync.pull.failed`` — ``provider.fetch_task(...)`` raised.

    Symmetric with :class:`SyncPushFailedPayload`: ``exception_type`` and
    ``exception_message`` are required.
    """

    action: Literal["sync.pull.failed"] = "sync.pull.failed"
    task_id: str
    external_id: str
    exception_type: str
    exception_message: str
    direction: Literal["pull"] = "pull"


class SyncConflictDetectedPayload(_SyncAuditBase):
    """Payload for ``sync.conflict_detected`` — emitted by ``_resolve_conflict``.

    Every conflict-resolution branch fires this event (manual_merge,
    local_wins, remote_wins, prompt_*).  ``strategy`` is the
    :class:`anvil.state.models.ConflictResolutionStrategy` value as
    a string; ``resolution`` is the short token describing which branch
    handled it (today: ``"local_wins_deferred"``, ``"remote_wins_deferred"``,
    ``"prompt_defaulted_to_local"``, ``"prompt_chose_local"``,
    ``"prompt_chose_remote"``, ``"prompt_skipped"``,
    ``"manual_merge_file_written"``, or
    ``"unknown_strategy:<value>"`` for the defensive ``else``).

    ``audit_note`` carries the merge file path on the ``manual_merge``
    branch; absent otherwise.

    NOT listed in the T3 plan's 10-action enumeration, but emitted today
    by ``cli/sync.py`` and registered in ``state/sqlite.py``'s dispatch
    table against ``SyncAuditPayload`` — omitting it would break dispatch
    the moment T3 lands.
    """

    action: Literal["sync.conflict_detected"] = "sync.conflict_detected"
    task_id: str
    external_id: str
    strategy: str
    resolution: str
    audit_note: str | None = None


class SyncReconciliationStartedPayload(_SyncAuditBase):
    """Payload for ``sync.reconciliation.started``.

    NO emitter today — included per the Phase 9 T3 plan for forward
    compatibility.  When ``ReconciliationEngine.scan()`` is wired to emit
    audit events (planned for a later phase) this is the start marker.

    ``provider_id`` is optional because a bare-reconciliation pass spans
    EVERY configured provider, not a single one.  When the
    reconciliation is scoped to a single provider, set it.
    """

    action: Literal["sync.reconciliation.started"] = "sync.reconciliation.started"
    audit_note: str | None = None


class SyncReconciliationCompletedPayload(_SyncAuditBase):
    """Payload for ``sync.reconciliation.completed``.

    NO emitter today — included per the Phase 9 T3 plan for forward
    compatibility.  Symmetric with
    :class:`SyncReconciliationStartedPayload`.  ``audit_note`` should
    carry the discrepancy summary in a single descriptive string
    (e.g. ``"orphaned_mappings=2 drift_sync_state=0"``).
    """

    action: Literal["sync.reconciliation.completed"] = "sync.reconciliation.completed"
    audit_note: str | None = None


# Discriminated union — Pydantic v2 dispatches O(1) on ``action``.
# Order is informational only; the discriminator field is what selects
# the concrete subclass at ``model_validate`` time.
SyncAuditPayload = Annotated[
    SyncBatchStartedPayload
    | SyncBatchCompletedPayload
    | SyncPushStartedPayload
    | SyncPushCompletedPayload
    | SyncPushDeferredPayload
    | SyncPushFailedPayload
    | SyncPullStartedPayload
    | SyncPullCompletedPayload
    | SyncPullDeferredPayload
    | SyncPullFailedPayload
    | SyncConflictDetectedPayload
    | SyncReconciliationStartedPayload
    | SyncReconciliationCompletedPayload,
    Field(discriminator="action"),
]
"""Backwards-compatible alias.

After Phase 9 T3 this is an ``Annotated[Union[...], Field(discriminator="action")]``
type form, NOT a ``BaseModel`` subclass.  Callers that need a callable
should use ``pydantic.TypeAdapter(SyncAuditPayload).validate_python(d)``
or look up the concrete subclass via :data:`ACTION_TO_PAYLOAD`.
"""


# Direct dispatcher lookup: ``action`` string → concrete subclass.
# ``state/sqlite.py:_apply_mutation`` should use this to map each
# ``sync.*`` event to its specific payload class; calling
# ``SubclassPayload.model_validate(payload_dict)`` works correctly even
# when the dict has no ``action`` key (the Literal default supplies it).
ACTION_TO_PAYLOAD: dict[str, type[BaseModel]] = {
    "sync.batch.started": SyncBatchStartedPayload,
    "sync.batch.completed": SyncBatchCompletedPayload,
    "sync.push.started": SyncPushStartedPayload,
    "sync.push.completed": SyncPushCompletedPayload,
    "sync.push.deferred": SyncPushDeferredPayload,
    "sync.push.failed": SyncPushFailedPayload,
    "sync.pull.started": SyncPullStartedPayload,
    "sync.pull.completed": SyncPullCompletedPayload,
    "sync.pull.deferred": SyncPullDeferredPayload,
    "sync.pull.failed": SyncPullFailedPayload,
    "sync.conflict_detected": SyncConflictDetectedPayload,
    "sync.reconciliation.started": SyncReconciliationStartedPayload,
    "sync.reconciliation.completed": SyncReconciliationCompletedPayload,
}


__all__ = [
    "ACTION_TO_PAYLOAD",
    "ClaimCreatedPayload",
    "ClaimReleasedPayload",
    "ClaimRenewedPayload",
    "ClaimStalePayload",
    "ConflictGroupUpsertedPayload",
    "EvidenceSubmittedPayload",
    "FeatureCreatedPayload",
    "FileChangedPayload",
    "PrdApprovedPayload",
    "PrdDecisionResolvedPayload",
    "PrdParsedPayload",
    "PrdReviewedPayload",
    "PrdRevisedPayload",
    "ProgressNotedPayload",
    "ProjectCreatedPayload",
    "StateInitializedPayload",
    "SyncAuditPayload",
    "SyncBatchCompletedPayload",
    "SyncBatchStartedPayload",
    "SyncConflictDetectedPayload",
    "SyncMappingDeletedPayload",
    "SyncMappingUpsertedPayload",
    "SyncPullCompletedPayload",
    "SyncPullDeferredPayload",
    "SyncPullFailedPayload",
    "SyncPullStartedPayload",
    "SyncPushCompletedPayload",
    "SyncPushDeferredPayload",
    "SyncPushFailedPayload",
    "SyncPushStartedPayload",
    "SyncReconciliationCompletedPayload",
    "SyncReconciliationStartedPayload",
    "TaskAppliedPayload",
    "TaskCreatedPayload",
    "TaskDeletedPayload",
    "TaskExpandedPayload",
    "TaskScoredPayload",
    "TaskStatusChangedPayload",
    "TaskSyncedFromRemotePayload",
    "FeatureDeletedPayload",
]
