"""DDL generation for fakoli-state SQLite schema.

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
"""

from __future__ import annotations

SCHEMA_VERSION: int = 4


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
    project_id                  TEXT PRIMARY KEY,
    status                      TEXT NOT NULL DEFAULT 'draft',
    summary                     TEXT NOT NULL DEFAULT '',
    goals                       TEXT NOT NULL DEFAULT '[]',
    non_goals                   TEXT NOT NULL DEFAULT '[]',
    requirements                TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria         TEXT NOT NULL DEFAULT '[]',
    risks                       TEXT NOT NULL DEFAULT '[]',
    open_questions              TEXT NOT NULL DEFAULT '[]',
    last_reviewed_at            TEXT,
    last_reviewed_by            TEXT
);

CREATE TABLE IF NOT EXISTS requirements (
    id                TEXT PRIMARY KEY,
    prd_section       TEXT NOT NULL,
    text              TEXT NOT NULL,
    source_paragraph  TEXT,
    derived           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS features (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'proposed',
    requirements TEXT NOT NULL DEFAULT '[]',
    tasks        TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    feature_id           TEXT NOT NULL REFERENCES features(id) ON DELETE RESTRICT,
    title                TEXT NOT NULL,
    description          TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'proposed',
    priority             TEXT NOT NULL DEFAULT 'medium',
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

CREATE TABLE IF NOT EXISTS sync_mappings (
    task_id                      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    external_system              TEXT NOT NULL,
    external_id                  TEXT NOT NULL,
    external_url                 TEXT,
    last_synced_at               TEXT NOT NULL,
    sync_state                   TEXT NOT NULL DEFAULT 'in_sync',
    conflict_resolution_strategy TEXT NOT NULL DEFAULT 'prompt',
    provider_metadata_json       TEXT,
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

PRAGMA user_version = 4;
"""


# Module-level constant so other modules can import without re-invoking the function.
DDL: str = generate_schema_sql()
