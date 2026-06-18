"""Brownfield scan / ingest (backlog T008).

Walks an existing working tree, builds a re-scannable *codebase model* persisted
in its own SQLite database (``.anvil/scan.db`` — kept separate from the
event-sourced ``state.db`` so the replay/audit guarantee is never touched), and
synthesises a draft ``prd.md`` plus an initial feature/task graph that the
existing parse → plan → score pipeline can seed offline.

Re-running a scan reconciles against the persisted model and reports the
*delta* (added / removed / changed files) rather than overwriting it.
"""

from __future__ import annotations

from anvil.scan.model import (
    CodebaseFile,
    CodebaseModel,
    ScanDelta,
    compute_delta,
    load_model,
    save_model,
    scan_working_tree,
)
from anvil.scan.prd_draft import draft_prd_from_model

__all__ = [
    "CodebaseFile",
    "CodebaseModel",
    "ScanDelta",
    "compute_delta",
    "draft_prd_from_model",
    "load_model",
    "save_model",
    "scan_working_tree",
]
