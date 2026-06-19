"""Typed per-step proof gate (T004) — a minimal, SL-3-aligned slice.

The runner asks one question per step: *does a passing `CommandProof` exist for
this requirement?* — not "did the executor claim success." A `Proof` requirement
(from the YAML, see :mod:`anvil.workflows.schema`) is satisfiable **only** by a
real :class:`CommandProof` whose `exit_code` is in `passing_exit_codes`. There is
no free-text fallback — that is the hole SL-3 closes, applied here to workflow
steps.

This is a scoped subset of the full SL-3 `ProofArtifact` design
(docs/specs/2026-06-19-sl3-proofartifact.md); names are chosen to be
forward-compatible so the runner can adopt the full type when it lands.
"""

from __future__ import annotations

from pydantic import BaseModel

from anvil.workflows.schema import Proof

__all__ = ["CommandProof", "proofs_satisfied", "requirement_satisfied"]


class CommandProof(BaseModel):
    """Evidence that a command ran, carrying its real exit code."""

    command: str
    exit_code: int


def requirement_satisfied(req: Proof, proofs: list[CommandProof]) -> bool:
    """True iff some CommandProof matches `req.command` with a passing exit code."""
    return any(
        p.command == req.command and p.exit_code in req.passing_exit_codes
        for p in proofs
    )


def proofs_satisfied(requirements: list[Proof], proofs: list[CommandProof]) -> bool:
    """True iff every requirement is satisfied by a passing CommandProof."""
    return all(requirement_satisfied(req, proofs) for req in requirements)
