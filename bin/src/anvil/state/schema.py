"""DDL generation for anvil SQLite schema.

Derives the schema from the Pydantic models in models.py.  Rules:
- One table per top-level entity; embedded value objects (Score, Verification)
  are JSON columns on their parent table (tasks).
- Type mapping: str→TEXT, int→INTEGER, datetime→TEXT (ISO 8601 UTC),
  bool→INTEGER (0/1), list[X]→TEXT (JSON), dict→TEXT (JSON),
  Pydantic embedded→TEXT (JSON), StrEnum→TEXT.
- CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS — always idempotent.
- PRAGMA user_version = N at the end for schema-version tracking.

Version history
---------------
- v1: Phase 2-7 schema (projects, prds, requirements, features, tasks,
  claims, evidence, decisions, reviews, events, conflict_groups). No
  sync_mappings table.
- v2: Phase 8 prep — sync_mappings table introduced (composite PK only;
  no UNIQUE on external_id).
- v3: Phase 8 ship — sync_mappings adds UNIQUE(external_system, external_id),
  external_url column, provider_metadata_json column, FK CASCADE direction
  flip. Migration: see docs/migrations.md (auto-upgrade on initialize for
  purely-additive changes).
- v4: v1.22.0 git-backed events Phase A — events.id CHECK widened to accept
  hash-chained ids (E-<hex>) alongside monotonic E{N}; events gains a
  nullable ``seq`` column (replay-assigned display order in git mode; NULL
  in local mode where the monotonic id IS the order). Auto-upgrade is
  additive (ALTER ADD seq); pre-v4 tables keep their strict id CHECK, which
  is harmless because local mode never writes hash ids and git mode always
  enters via a full projection rebuild that recreates the table from this DDL.
- v5: non-feature task types (T015) — tasks gains a ``task_type`` column
  (TEXT, NOT NULL DEFAULT 'feature') so a brownfield PRD can describe
  bugfix / refactor / modify work alongside greenfield feature tasks.
  Auto-upgrade is purely additive (ALTER ADD task_type with a DEFAULT, which
  backfills every existing row to 'feature' — exactly the pre-v5 meaning).
- v6: SL-3 / B48 typed proofs — evidence gains a ``proofs`` column
  (TEXT NOT NULL DEFAULT '[]'). Purely additive; the DEFAULT backfills every
  existing evidence row to "no typed proofs," the correct pre-SL-3 meaning.
- v7: v0.3 multi-PRD persistence foundation (T002). The singleton ``prds``
  table becomes a multi-row table keyed on a single-column ``id`` PRIMARY KEY
  (NOT composite) and gains title/target_version/target_tag/is_default/
  created_at/updated_at; a partial unique index ``ux_prds_default`` enforces
  at most one default PRD. ``requirements``/``features``/``tasks`` each gain a
  ``prd_id`` partition column (TEXT NOT NULL DEFAULT 'default'); ``requirements``
  also gains nullable ``revision_introduced``/``revision_superseded`` lineage
  columns. ``sync_mappings`` gains ``prd_id``/``entity_kind`` partition columns.
  New indexes: idx_requirements_prd, idx_features_prd, idx_tasks_prd_status
  (prd_id, status). The v6->v7 in-place migration rebuilds ``prds`` (SQLite
  cannot ALTER a PRIMARY KEY) and ALTER-backfills every other column with a
  DEFAULT so existing rows adopt the default PRD with zero data loss. Every new
  column has a DEFAULT so the existing INSERT statements (which never mention
  the new columns) keep working unchanged — Phase 0 is purely additive at the
  write layer.
"""

from __future__ import annotations

SCHEMA_VERSION: int = 7


def get_schema_version() -> int:
    """Return the schema version this build of the code targets.

    T007/B11: a small public accessor over the existing ``SCHEMA_VERSION``
    constant so tooling (the ``status`` command, MCP hosts, external scripts)
    can read the version without importing the module-level constant directly.
    This is intentionally a thin getter — it does NOT introduce a new
    versioning scheme; ``SCHEMA_VERSION`` (and the ``PRAGMA user_version``
    stamped on the DB) remain the single source of truth.
    """
    return SCHEMA_VERSION


def generate_schema_sql() -> str:  # noqa: PLR0915  (acceptable length for DDL)
    """Return the full CREATE TABLE + CREATE INDEX script.

    The result is idempotent: safe to run against an existing database.
    Foreign-key constraints use RESTRICT only where cascade-delete would be
    data-loss dangerous (tasks, claims, evidence).  Lookup tables (decisions,
    reviews, events, conflict_groups) use no explicit ON DELETE clause,
    defaulting to RESTRICT, which is acceptable for Phase 2 where deletes
    are not yet implemented.  sync_mappings uses ON DELETE CASCADE so
    dropping a task automatically drops its external mappings (Phase 8
    direction flip — RESTRICT would have wedged any future task.deleted
    pathway against synced tasks).
    """
    return """\
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prds (
    id                          TEXT PRIMARY KEY DEFAULT 'default',
    project_id                  TEXT NOT NULL DEFAULT '',
    title                       TEXT NOT NULL DEFAULT '',
    status                      TEXT NOT NULL DEFAULT 'draft',
    summary                     TEXT NOT NULL DEFAULT '',
    goals                       TEXT NOT NULL DEFAULT '[]',
    non_goals                   TEXT NOT NULL DEFAULT '[]',
    requirements                TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria         TEXT NOT NULL DEFAULT '[]',
    risks                       TEXT NOT NULL DEFAULT '[]',
    open_questions              TEXT NOT NULL DEFAULT '[]',
    last_reviewed_at            TEXT,
    last_reviewed_by            TEXT,
    target_version              TEXT,
    target_tag                  TEXT,
    is_default                  INTEGER NOT NULL DEFAULT 0,
    created_at                  TEXT,
    updated_at                  TEXT
);

-- At most one default PRD per project (single-row today, partition-ready).
CREATE UNIQUE INDEX IF NOT EXISTS ux_prds_default ON prds (project_id)
    WHERE is_default = 1;

CREATE TABLE IF NOT EXISTS requirements (
    id                   TEXT PRIMARY KEY,
    prd_id               TEXT NOT NULL DEFAULT 'default',
    prd_section          TEXT NOT NULL,
    text                 TEXT NOT NULL,
    source_paragraph     TEXT,
    derived              INTEGER NOT NULL DEFAULT 0,
    revision_introduced  INTEGER,
    revision_superseded  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_requirements_prd ON requirements (prd_id);

CREATE TABLE IF NOT EXISTS features (
    id           TEXT PRIMARY KEY,
    prd_id       TEXT NOT NULL DEFAULT 'default',
    title        TEXT NOT NULL,
    description  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'proposed',
    requirements TEXT NOT NULL DEFAULT '[]',
    tasks        TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_features_prd ON features (prd_id);

CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    feature_id           TEXT NOT NULL REFERENCES features(id) ON DELETE RESTRICT,
    prd_id               TEXT NOT NULL DEFAULT 'default',
    title                TEXT NOT NULL,
    description          TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'proposed',
    priority             TEXT NOT NULL DEFAULT 'medium',
    task_type            TEXT NOT NULL DEFAULT 'feature',
    dependencies         TEXT NOT NULL DEFAULT '[]',
    conflict_groups      TEXT NOT NULL DEFAULT '[]',
    scores               TEXT NOT NULL DEFAULT '{}',
    acceptance_criteria  TEXT NOT NULL DEFAULT '[]',
    implementation_notes TEXT NOT NULL DEFAULT '[]',
    verification         TEXT NOT NULL DEFAULT '{}',
    likely_files         TEXT NOT NULL DEFAULT '[]',
    parent_task_id       TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);

CREATE INDEX IF NOT EXISTS idx_tasks_feature_status ON tasks (feature_id, status);

CREATE INDEX IF NOT EXISTS idx_tasks_prd_status ON tasks (prd_id, status);

CREATE TABLE IF NOT EXISTS claims (
    id                 TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    claimed_by         TEXT NOT NULL,
    claim_type         TEXT NOT NULL DEFAULT 'task',
    status             TEXT NOT NULL DEFAULT 'active',
    branch             TEXT,
    worktree_path      TEXT,
    expected_files     TEXT NOT NULL DEFAULT '[]',
    created_at         TEXT NOT NULL,
    lease_expires_at   TEXT NOT NULL,
    last_heartbeat_at  TEXT NOT NULL,
    released_at        TEXT,
    release_reason     TEXT
);

CREATE INDEX IF NOT EXISTS idx_claims_task_status ON claims (task_id, status);

CREATE TABLE IF NOT EXISTS evidence (
    id                  TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    claim_id            TEXT NOT NULL REFERENCES claims(id) ON DELETE RESTRICT,
    commands_run        TEXT NOT NULL DEFAULT '[]',
    output_excerpt      TEXT,
    files_changed       TEXT NOT NULL DEFAULT '[]',
    pr_url              TEXT,
    commit_sha          TEXT,
    screenshots         TEXT NOT NULL DEFAULT '[]',
    known_limitations   TEXT,
    proofs              TEXT NOT NULL DEFAULT '[]',
    submitted_at        TEXT NOT NULL,
    submitted_by        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    context          TEXT NOT NULL,
    decision         TEXT NOT NULL,
    consequences     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    related_tasks    TEXT NOT NULL DEFAULT '[]',
    related_features TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS reviews (
    id           TEXT PRIMARY KEY,
    target_kind  TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    reviewed_by  TEXT NOT NULL,
    decision     TEXT NOT NULL,
    notes        TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_target ON reviews (target_kind, target_id);

CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY CHECK (id GLOB 'E[0-9]*' OR id GLOB 'E-*'),
    timestamp    TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    target_kind  TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    seq          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp);

-- ``task_id`` is still NOT NULL: T026 is the data-only phase, so only task-kind
-- mappings persist. The SyncMapping model accepts an entity_kind='prd'
-- (milestone) shape with a NULL task_id, but that is rejected up front by
-- ``_check_sync_mapping_upserted`` until this column is relaxed in the milestone
-- phase — see that gate for the fail-fast rationale.
CREATE TABLE IF NOT EXISTS sync_mappings (
    task_id                      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    external_system              TEXT NOT NULL,
    external_id                  TEXT NOT NULL,
    external_url                 TEXT,
    last_synced_at               TEXT NOT NULL,
    sync_state                   TEXT NOT NULL DEFAULT 'in_sync',
    conflict_resolution_strategy TEXT NOT NULL DEFAULT 'prompt',
    provider_metadata_json       TEXT,
    prd_id                       TEXT,
    entity_kind                  TEXT NOT NULL DEFAULT 'task',
    PRIMARY KEY (task_id, external_system),
    UNIQUE (external_system, external_id)
);

CREATE INDEX IF NOT EXISTS idx_sync_mappings_external
    ON sync_mappings (external_system, external_id);

CREATE TABLE IF NOT EXISTS conflict_groups (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    task_ids TEXT NOT NULL DEFAULT '[]',
    reason   TEXT NOT NULL
);

-- Informational only: ``_apply_ddl`` strips this line and stamps the version
-- from ``SCHEMA_VERSION`` at runtime, but keep it in lockstep with the constant
-- so anyone running this DDL by hand gets the right version.
PRAGMA user_version = 7;
"""


# Module-level constant so other modules can import without re-invoking the function.
DDL: str = generate_schema_sql()
