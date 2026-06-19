"""Typed models for the WF-3 declarative workflow format.

Mirrors the locked primitive set in docs/decisions/wf3-format.md:
`run`, `fan_out`, `needs`, `proof`, `on_fail: reopen`, and the `uses_code`
escape-hatch. No richer control flow (conditionals/loops) is modelled — that is
the deliberate ceiling that keeps the format declarative rather than a DAG engine.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Proof(BaseModel):
    """A typed per-step gate: a passing CommandProof must exist (SL-3)."""

    command: str
    passing_exit_codes: list[int] = Field(default_factory=lambda: [0])


class Step(BaseModel):
    """One workflow step. `run` carries the action; `uses_code` is the escape
    hatch a step uses instead when declarative is insufficient."""

    id: str
    run: str | None = None
    needs: list[str] = Field(default_factory=list)
    fan_out: str | None = None
    proof: list[Proof] = Field(default_factory=list)
    on_fail: str | None = None
    uses_code: str | None = None


class Workflow(BaseModel):
    """A parsed workflow file: an ordered list of steps plus metadata.

    `trigger` is recorded verbatim but not acted on — triggering is delegated to
    the harness (docs/decisions/wf3-format.md)."""

    name: str
    description: str | None = None
    trigger: dict[str, object] | None = None
    steps: list[Step]
