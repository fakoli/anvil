"""Deterministic parser for `.anvil/workflows/*.yaml` — no LLM, no execution.

Turns a workflow YAML file into a typed :class:`Workflow`. Unlike the PRD parser
(``planning/template.py``), this fails fast and raises rather than collecting
errors:

    # ponytail: fail-fast, naming the offending step — a workflow file is small,
    # so an invalid one has nothing partial worth surfacing the way a PRD does.

The allowed-key set IS the control-flow ceiling locked in
docs/decisions/wf3-format.md: an unknown key (``when``, ``loop``, …) is rejected
so the format cannot drift into a DAG engine.
"""

from __future__ import annotations

import yaml

from anvil.workflows.schema import Proof, Step, Workflow

__all__ = ["WorkflowParseError", "parse_workflow"]

# Locked primitive set (docs/decisions/wf3-format.md §"Locked primitive set").
_ALLOWED_STEP_KEYS = {"id", "run", "needs", "fan_out", "proof", "on_fail", "uses_code"}
_ALLOWED_ON_FAIL = {"reopen"}


class WorkflowParseError(Exception):
    """A workflow YAML file failed validation; the message names the bad step."""


def parse_workflow(text: str) -> Workflow:
    """Parse and validate a workflow YAML document into a typed ``Workflow``.

    Raises :class:`WorkflowParseError` (naming the offending step where possible)
    on any structural problem.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise WorkflowParseError("workflow must be a YAML mapping with 'name' and 'steps'")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WorkflowParseError("workflow missing required field: name")

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowParseError("workflow missing required field: steps (non-empty list)")

    steps: list[Step] = []
    seen_ids: set[str] = set()
    for idx, rs in enumerate(raw_steps):
        where = f"step #{idx + 1}"
        if not isinstance(rs, dict):
            raise WorkflowParseError(f"{where}: must be a mapping")

        sid = rs.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise WorkflowParseError(f"{where}: missing required field: id")
        where = f"step '{sid}'"
        if sid in seen_ids:
            raise WorkflowParseError(f"{where}: duplicate step id")
        seen_ids.add(sid)

        extra = set(rs) - _ALLOWED_STEP_KEYS
        if extra:
            raise WorkflowParseError(
                f"{where}: unknown key(s) {sorted(extra)}; "
                f"allowed: {sorted(_ALLOWED_STEP_KEYS)}"
            )

        if not rs.get("run") and not rs.get("uses_code"):
            raise WorkflowParseError(f"{where}: missing required field: run (or uses_code)")

        on_fail = rs.get("on_fail")
        if on_fail is not None and on_fail not in _ALLOWED_ON_FAIL:
            raise WorkflowParseError(
                f"{where}: on_fail must be one of {sorted(_ALLOWED_ON_FAIL)}, got {on_fail!r}"
            )

        needs = rs.get("needs") or []
        if not isinstance(needs, list) or not all(isinstance(n, str) for n in needs):
            raise WorkflowParseError(f"{where}: needs must be a list of step ids")

        steps.append(
            Step(
                id=sid,
                run=rs.get("run"),
                needs=needs,
                fan_out=rs.get("fan_out"),
                proof=_parse_proofs(where, rs.get("proof")),
                on_fail=on_fail,
                uses_code=rs.get("uses_code"),
            )
        )

    # Cross-step check: every `needs` must reference a declared step id.
    for step in steps:
        for dep in step.needs:
            if dep not in seen_ids:
                raise WorkflowParseError(
                    f"step '{step.id}': needs references unknown step id '{dep}'"
                )

    return Workflow(
        name=name,
        description=raw.get("description"),
        trigger=raw.get("trigger"),
        steps=steps,
    )


def _parse_proofs(where: str, raw: object) -> list[Proof]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WorkflowParseError(f"{where}: proof must be a list")
    proofs: list[Proof] = []
    for p in raw:
        if not isinstance(p, dict) or not p.get("command"):
            raise WorkflowParseError(f"{where}: each proof needs a 'command'")
        proofs.append(
            Proof(command=p["command"], passing_exit_codes=p.get("passing_exit_codes", [0]))
        )
    return proofs
