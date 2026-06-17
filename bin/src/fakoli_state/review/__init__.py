"""Review sub-package for fakoli-state.

Provides gate functions used by the CLI ``apply`` command to validate
Evidence before a human approves a task transition.
"""

from fakoli_state.review.gates import (
    DeferredFinding,
    deferred_findings,
    deferred_findings_for_files,
    evidence_complete,
)

__all__ = [
    "DeferredFinding",
    "deferred_findings",
    "deferred_findings_for_files",
    "evidence_complete",
]
