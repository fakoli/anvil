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
"""

from __future__ import annotations

SCHEMA_VERSION: int = 5


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

PRAGMA user_version = 5;
"""


# Module-level constant so other modules can import without re-invoking the function.
DDL: str = generate_schema_sql()


def generate_schema_sql_mysql() -> list[str]:  # noqa: PLR0915
    """Return the MySQL 8.0 DDL as a list of individual CREATE TABLE statements.

    Mirrors :func:`generate_schema_sql` table-for-table but emits the MySQL
    dialect (see docs/specs/2026-06-18-mysql-backend.md §2.5). Differences from
    the SQLite generator, all mechanical:

    - ``TEXT PRIMARY KEY`` → ``VARCHAR(64)`` (MySQL cannot index/PK a TEXT
      column without a prefix length; entity ids are short, fixed-shape).
    - JSON columns stay ``LONGTEXT`` (NOT native ``JSON``) so they round-trip
      as JSON **strings**, exactly like SQLite. The inherited row-converters in
      ``MySQLBackend`` do their own ``json.loads`` — switching to native JSON
      would hand them parsed objects and break that shared code path.
    - ``DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin`` on every table so id and
      string comparisons are case- and accent-SENSITIVE, matching SQLite's
      default BINARY collation. A case-insensitive collation would make
      ``T001``/``t001`` collide and break the ``id != %s`` self-skip in the
      claim guard (§2.5, the load-bearing collation note).
    - Indexes are declared INLINE in ``CREATE TABLE`` (MySQL lacks a reliable
      ``CREATE INDEX IF NOT EXISTS``); ``CREATE TABLE IF NOT EXISTS`` keeps the
      whole statement idempotent.
    - The events ``id`` GLOB CHECK is dropped — the id format is produced by
      code, and the cross-host single-winner correctness does not depend on it.
    - The ``claims`` table grows a STORED generated column ``active_task_id``
      plus ``UNIQUE KEY uq_one_active_claim_per_task`` — the engine-enforced,
      host-independent single-winner backstop (§0/§2.2). NULL when the claim is
      not ``active``; UNIQUE ignores NULLs, so any number of released/stale
      claims coexist while at most one ``active`` row per ``task_id`` can exist.
    - A one-row ``schema_version`` table replaces ``PRAGMA user_version``.

    Returned as a list (not a single ``;``-joined script) because PyMySQL's
    ``cursor.execute`` runs one statement at a time.
    """
    charset = "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"
    return [
        f"""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INT NOT NULL,
            PRIMARY KEY (version)
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS event_counter (
            id     TINYINT NOT NULL,
            n      BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (id)
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS projects (
            id          VARCHAR(64) NOT NULL,
            name        TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at  VARCHAR(40) NOT NULL,
            updated_at  VARCHAR(40) NOT NULL,
            PRIMARY KEY (id)
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS prds (
            project_id        VARCHAR(64) NOT NULL,
            status            VARCHAR(32) NOT NULL DEFAULT 'draft',
            summary           LONGTEXT NOT NULL,
            goals             LONGTEXT NOT NULL,
            non_goals         LONGTEXT NOT NULL,
            requirements      LONGTEXT NOT NULL,
            acceptance_criteria LONGTEXT NOT NULL,
            risks             LONGTEXT NOT NULL,
            open_questions    LONGTEXT NOT NULL,
            last_reviewed_at  VARCHAR(40),
            last_reviewed_by  VARCHAR(255),
            PRIMARY KEY (project_id)
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS requirements (
            id               VARCHAR(64) NOT NULL,
            prd_section      TEXT NOT NULL,
            text             LONGTEXT NOT NULL,
            source_paragraph LONGTEXT,
            derived          INT NOT NULL DEFAULT 0,
            PRIMARY KEY (id)
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS features (
            id           VARCHAR(64) NOT NULL,
            title        TEXT NOT NULL,
            description  TEXT NOT NULL,
            status       VARCHAR(32) NOT NULL DEFAULT 'proposed',
            requirements LONGTEXT NOT NULL,
            tasks        LONGTEXT NOT NULL,
            PRIMARY KEY (id)
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS tasks (
            id                   VARCHAR(64) NOT NULL,
            feature_id           VARCHAR(64) NOT NULL,
            title                TEXT NOT NULL,
            description          LONGTEXT NOT NULL,
            status               VARCHAR(32) NOT NULL DEFAULT 'proposed',
            priority             VARCHAR(32) NOT NULL DEFAULT 'medium',
            task_type            VARCHAR(32) NOT NULL DEFAULT 'feature',
            dependencies         LONGTEXT NOT NULL,
            conflict_groups      LONGTEXT NOT NULL,
            scores               LONGTEXT NOT NULL,
            acceptance_criteria  LONGTEXT NOT NULL,
            implementation_notes LONGTEXT NOT NULL,
            verification         LONGTEXT NOT NULL,
            likely_files         LONGTEXT NOT NULL,
            parent_task_id       VARCHAR(64),
            created_at           VARCHAR(40) NOT NULL,
            updated_at           VARCHAR(40) NOT NULL,
            PRIMARY KEY (id),
            KEY idx_tasks_status (status),
            KEY idx_tasks_feature_status (feature_id, status),
            CONSTRAINT fk_tasks_feature FOREIGN KEY (feature_id)
                REFERENCES features (id) ON DELETE RESTRICT,
            CONSTRAINT fk_tasks_parent FOREIGN KEY (parent_task_id)
                REFERENCES tasks (id) ON DELETE SET NULL
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS claims (
            id                 VARCHAR(64) NOT NULL,
            task_id            VARCHAR(64) NOT NULL,
            claimed_by         VARCHAR(255) NOT NULL,
            claim_type         VARCHAR(32) NOT NULL DEFAULT 'task',
            status             VARCHAR(32) NOT NULL DEFAULT 'active',
            branch             TEXT,
            worktree_path      TEXT,
            expected_files     LONGTEXT NOT NULL,
            created_at         VARCHAR(40) NOT NULL,
            lease_expires_at   VARCHAR(40) NOT NULL,
            last_heartbeat_at  VARCHAR(40) NOT NULL,
            released_at        VARCHAR(40),
            release_reason     TEXT,
            active_task_id     VARCHAR(64)
                AS (CASE WHEN status = 'active' THEN task_id ELSE NULL END) STORED,
            PRIMARY KEY (id),
            KEY idx_claims_task_status (task_id, status),
            UNIQUE KEY uq_one_active_claim_per_task (active_task_id),
            CONSTRAINT fk_claims_task FOREIGN KEY (task_id)
                REFERENCES tasks (id) ON DELETE RESTRICT
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS evidence (
            id                VARCHAR(64) NOT NULL,
            task_id           VARCHAR(64) NOT NULL,
            claim_id          VARCHAR(64) NOT NULL,
            commands_run      LONGTEXT NOT NULL,
            output_excerpt    LONGTEXT,
            files_changed     LONGTEXT NOT NULL,
            pr_url            TEXT,
            commit_sha        VARCHAR(255),
            screenshots       LONGTEXT NOT NULL,
            known_limitations LONGTEXT,
            submitted_at      VARCHAR(40) NOT NULL,
            submitted_by      VARCHAR(255) NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT fk_evidence_task FOREIGN KEY (task_id)
                REFERENCES tasks (id) ON DELETE RESTRICT,
            CONSTRAINT fk_evidence_claim FOREIGN KEY (claim_id)
                REFERENCES claims (id) ON DELETE RESTRICT
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS decisions (
            id               VARCHAR(64) NOT NULL,
            title            TEXT NOT NULL,
            context          LONGTEXT NOT NULL,
            decision         LONGTEXT NOT NULL,
            consequences     LONGTEXT NOT NULL,
            created_at       VARCHAR(40) NOT NULL,
            related_tasks    LONGTEXT NOT NULL,
            related_features LONGTEXT NOT NULL,
            PRIMARY KEY (id)
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS reviews (
            id          VARCHAR(64) NOT NULL,
            target_kind VARCHAR(32) NOT NULL,
            target_id   VARCHAR(64) NOT NULL,
            reviewed_by VARCHAR(255) NOT NULL,
            decision    VARCHAR(32) NOT NULL,
            notes       LONGTEXT,
            created_at  VARCHAR(40) NOT NULL,
            PRIMARY KEY (id),
            KEY idx_reviews_target (target_kind, target_id)
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS events (
            id           VARCHAR(64) NOT NULL,
            timestamp    VARCHAR(40) NOT NULL,
            actor        VARCHAR(255) NOT NULL,
            action       VARCHAR(64) NOT NULL,
            target_kind  VARCHAR(32) NOT NULL,
            target_id    VARCHAR(64) NOT NULL,
            payload_json LONGTEXT NOT NULL,
            seq          INT,
            PRIMARY KEY (id),
            KEY idx_events_timestamp (timestamp)
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS sync_mappings (
            task_id                      VARCHAR(64) NOT NULL,
            external_system              VARCHAR(64) NOT NULL,
            external_id                  VARCHAR(255) NOT NULL,
            external_url                 TEXT,
            last_synced_at               VARCHAR(40) NOT NULL,
            sync_state                   VARCHAR(32) NOT NULL DEFAULT 'in_sync',
            conflict_resolution_strategy VARCHAR(32) NOT NULL DEFAULT 'prompt',
            provider_metadata_json       LONGTEXT,
            PRIMARY KEY (task_id, external_system),
            UNIQUE KEY uq_sync_external (external_system, external_id),
            KEY idx_sync_mappings_external (external_system, external_id),
            CONSTRAINT fk_sync_task FOREIGN KEY (task_id)
                REFERENCES tasks (id) ON DELETE CASCADE
        ) ENGINE=InnoDB {charset}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS conflict_groups (
            id       VARCHAR(64) NOT NULL,
            name     TEXT NOT NULL,
            task_ids LONGTEXT NOT NULL,
            reason   LONGTEXT NOT NULL,
            PRIMARY KEY (id)
        ) ENGINE=InnoDB {charset}
        """,
    ]


# Module-level constant: the MySQL DDL statement list. Imported by the MySQL
# backend the same way ``DDL`` is imported by the SQLite backend.
DDL_MYSQL: list[str] = generate_schema_sql_mysql()
