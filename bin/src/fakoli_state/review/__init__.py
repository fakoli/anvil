"""Review sub-package for fakoli-state.

Provides gate functions used by the CLI ``apply`` command to validate
Evidence before a human approves a task transition.
"""

from fakoli_state.review.gates import evidence_complete

__all__ = ["evidence_complete"]
