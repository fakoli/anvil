"""Codebase model: walk a working tree, persist it, and diff re-scans.

This is the queryable, re-scannable side of the T008 brownfield scan. It is
deliberately decoupled from the event-sourced ``state.db``:

* :func:`scan_working_tree` walks the project root (preferring ``git ls-files``
  so ``.gitignore`` is honoured, falling back to an ``os.walk`` that prunes
  the obvious noise directories) and returns a :class:`CodebaseModel` — a flat
  set of :class:`CodebaseFile` rows grouped by top-level component and language.
* :func:`save_model` / :func:`load_model` persist the model in a *separate*
  SQLite database (``.anvil/scan.db``). Keeping it out of ``state.db``
  means the codebase model never participates in event replay and cannot break
  the SL-1 replay-equivalence guarantee.
* :func:`compute_delta` diffs a freshly-scanned model against the persisted one
  and returns a :class:`ScanDelta` (added / removed / changed / unchanged) so a
  re-scan reports what moved instead of silently overwriting.

Everything here is pure-Python + stdlib ``sqlite3``; there is no dependency on
the rest of the engine, so it is cheap to unit-test in isolation.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

__all__ = [
    "SCAN_DB_NAME",
    "CodebaseFile",
    "CodebaseModel",
    "ScanDelta",
    "compute_delta",
    "load_model",
    "save_model",
    "scan_working_tree",
]

# The codebase model lives in its OWN db, beside state.db, inside .anvil/.
SCAN_DB_NAME = "scan.db"

_GIT_TIMEOUT_SECONDS = 10

# Directories that never belong in a codebase model. Pruned from the os.walk
# fallback (git ls-files already excludes them via .gitignore in most repos).
_PRUNE_DIRS = frozenset(
    {
        ".git",
        ".anvil",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".tox",
        ".idea",
        ".vscode",
        "target",
        ".next",
        ".cache",
    }
)

# Extension → coarse language label. Unknown extensions map to "other"; this is
# intentionally a small, stable map — the scan is a seed, not a linter.
_EXT_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".swift": "swift",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".md": "markdown",
    ".rst": "docs",
    ".txt": "docs",
    ".json": "config",
    ".yaml": "config",
    ".yml": "config",
    ".toml": "config",
    ".ini": "config",
    ".cfg": "config",
    ".html": "web",
    ".css": "web",
    ".scss": "web",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class CodebaseFile(BaseModel):
    """A single tracked source file in the codebase model."""

    path: str  # POSIX-style, relative to project root
    component: str  # top-level directory (or "(root)" for root-level files)
    language: str
    size_bytes: int
    content_hash: str  # sha256 hex of the file bytes, or "" when unreadable


class CodebaseModel(BaseModel):
    """The full codebase model: every scanned file plus rollup summaries."""

    files: list[CodebaseFile] = Field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.files)

    def components(self) -> dict[str, list[CodebaseFile]]:
        """Group files by their top-level component, sorted by path."""
        groups: dict[str, list[CodebaseFile]] = {}
        for f in self.files:
            groups.setdefault(f.component, []).append(f)
        for bucket in groups.values():
            bucket.sort(key=lambda f: f.path)
        return dict(sorted(groups.items()))

    def language_counts(self) -> dict[str, int]:
        """Return ``{language: file_count}`` sorted by descending count."""
        counts: dict[str, int] = {}
        for f in self.files:
            counts[f.language] = counts.get(f.language, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


class ScanDelta(BaseModel):
    """The difference between a freshly-scanned model and a persisted one."""

    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)


# ---------------------------------------------------------------------------
# Walking the working tree
# ---------------------------------------------------------------------------


def _component_of(rel_posix: str) -> str:
    """Top-level component for a relative POSIX path ('(root)' for root files)."""
    head, _, tail = rel_posix.partition("/")
    return head if tail else "(root)"


def _language_of(rel_posix: str) -> str:
    return _EXT_LANGUAGE.get(Path(rel_posix).suffix.lower(), "other")


def _hash_file(abs_path: Path) -> tuple[int, str]:
    """Return ``(size_bytes, sha256_hex)`` for *abs_path*; ('', '') on error."""
    try:
        data = abs_path.read_bytes()
    except OSError:
        return 0, ""
    return len(data), hashlib.sha256(data).hexdigest()


def _git_tracked_files(root: Path) -> list[str] | None:
    """Return git-known relative paths, or None when git is unavailable.

    Uses ``git ls-files`` with ``--cached`` (tracked) plus ``--others
    --exclude-standard`` (untracked-but-not-ignored), so a freshly-added file
    that has not been committed yet is still scanned, while everything in
    ``.gitignore`` (``.venv/``, ``node_modules/``, build dirs, …) is excluded.
    Returns None (caller falls back to os.walk) when git is missing, the
    directory is not a repo, or git errors/times out.
    """
    if shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            cwd=str(root),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.decode("utf-8", errors="replace")
    return [p for p in raw.split("\0") if p]


def _walk_files(root: Path) -> list[str]:
    """Fallback file walk that prunes noise dirs. Returns relative POSIX paths."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in-place so os.walk does not descend into noise directories.
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for name in filenames:
            abs_path = Path(dirpath) / name
            try:
                rel = abs_path.relative_to(root)
            except ValueError:
                continue
            found.append(rel.as_posix())
    return found


def scan_working_tree(root: Path) -> CodebaseModel:
    """Walk *root* and return the :class:`CodebaseModel` for its source files.

    Prefers ``git ls-files`` (respects .gitignore) and falls back to a pruned
    ``os.walk``. Files under any pruned directory are excluded either way. The
    returned model is sorted by path for stable output.
    """
    root = root.resolve()
    rels = _git_tracked_files(root)
    if rels is None:
        rels = _walk_files(root)

    files: list[CodebaseFile] = []
    for rel in sorted(set(rels)):
        # Defensive: git can list paths under a pruned dir if it is tracked
        # (rare). Skip anything whose first segment is a noise dir.
        first = rel.split("/", 1)[0]
        if first in _PRUNE_DIRS:
            continue
        abs_path = root / rel
        if not abs_path.is_file():
            continue
        size, digest = _hash_file(abs_path)
        files.append(
            CodebaseFile(
                path=rel,
                component=_component_of(rel),
                language=_language_of(rel),
                size_bytes=size,
                content_hash=digest,
            )
        )
    return CodebaseModel(files=files)


# ---------------------------------------------------------------------------
# Persistence (separate scan.db)
# ---------------------------------------------------------------------------


_SCAN_DDL = """\
CREATE TABLE IF NOT EXISTS codebase_files (
    path          TEXT PRIMARY KEY,
    component     TEXT NOT NULL,
    language      TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    content_hash  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_codebase_files_component
    ON codebase_files (component);

CREATE INDEX IF NOT EXISTS idx_codebase_files_language
    ON codebase_files (language);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCAN_DDL)
    return conn


def save_model(model: CodebaseModel, db_path: Path) -> None:
    """Persist *model* to the scan db at *db_path*, replacing any prior rows.

    The scan db is a snapshot of the latest scan — a re-scan replaces the rows
    after :func:`compute_delta` has already reported the diff against the old
    rows, so no information is lost (the delta is the durable record of change,
    surfaced to the user / JSON envelope).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM codebase_files")
        conn.executemany(
            "INSERT INTO codebase_files "
            "(path, component, language, size_bytes, content_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (f.path, f.component, f.language, f.size_bytes, f.content_hash)
                for f in model.files
            ],
        )
        conn.commit()
    finally:
        conn.close()


def load_model(db_path: Path) -> CodebaseModel | None:
    """Load the persisted codebase model, or None if no scan db exists yet."""
    if not db_path.exists():
        return None
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT path, component, language, size_bytes, content_hash "
            "FROM codebase_files ORDER BY path"
        ).fetchall()
    finally:
        conn.close()
    return CodebaseModel(
        files=[
            CodebaseFile(
                path=r[0],
                component=r[1],
                language=r[2],
                size_bytes=r[3],
                content_hash=r[4],
            )
            for r in rows
        ]
    )


# ---------------------------------------------------------------------------
# Delta
# ---------------------------------------------------------------------------


def compute_delta(old: CodebaseModel | None, new: CodebaseModel) -> ScanDelta:
    """Diff *new* against *old* (None == first scan: everything is added)."""
    old_map = {f.path: f.content_hash for f in (old.files if old else [])}
    new_map = {f.path: f.content_hash for f in new.files}

    added: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    for path, digest in new_map.items():
        if path not in old_map:
            added.append(path)
        elif old_map[path] != digest:
            changed.append(path)
        else:
            unchanged.append(path)
    removed = [p for p in old_map if p not in new_map]

    return ScanDelta(
        added=sorted(added),
        removed=sorted(removed),
        changed=sorted(changed),
        unchanged=sorted(unchanged),
    )
