"""Canonical PRD source-to-event persistence planning.

This is the single producer used by the CLI, MCP, and planning batch paths.
It consumes the exact source bytes qualified by provider-read operation
``state.prd.content`` v1 (merged in provider-read-contracts at
``09914b40ffa56900cb47e6990fd62b5e42b212fc``) and binds every material change
to one revision/digest tuple.  Byte-identical persisted content is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from anvil.read_contracts import (
    PRD_CONTENT_OPERATION_ID,
    PRD_CONTENT_OPERATION_VERSION,
    PRD_CONTENT_SCHEMA_ID,
)
from anvil.state.models import EventDraft

if TYPE_CHECKING:
    from anvil.clock import Clock
    from anvil.planning.template import ParseResult
    from anvil.state.backend import Backend


PROVIDER_READS_MERGE_COMMIT = "09914b40ffa56900cb47e6990fd62b5e42b212fc"
PROVIDER_PRD_CONTENT_OPERATION = (
    PRD_CONTENT_OPERATION_ID,
    PRD_CONTENT_OPERATION_VERSION,
    PRD_CONTENT_SCHEMA_ID,
)


class ExactPrdSource(Protocol):
    source_bytes: bytes
    markdown: str
    source_sha256: str
    source_size_bytes: int
    source_encoding: str


class PrdRevisionError(ValueError):
    """A deterministic source-to-revision refusal."""


@dataclass(frozen=True, slots=True)
class PrdPersistencePlan:
    action: Literal["parsed", "revised", "unchanged"]
    draft: EventDraft | None
    revision: int
    source_sha256: str
    status: str


def source_binding(source: ExactPrdSource, revision: int) -> dict[str, object]:
    """Return the complete revision-bound tuple from one exact source read."""
    return {
        "source_text": source.markdown,
        "source_sha256": source.source_sha256,
        "source_size_bytes": source.source_size_bytes,
        "source_encoding": source.source_encoding,
        "source_revision": revision,
        "provenance_state": "available",
        "content_available": True,
    }


def build_prd_persistence_plan(
    backend: Backend,
    parsed: ParseResult,
    source: ExactPrdSource,
    *,
    project_id: str,
    is_default: bool,
    actor: str,
    clock: Clock,
) -> PrdPersistencePlan:
    """Build exactly one create/revision event, or an exact-byte no-op."""
    stored_prd_id = parsed.prd.id
    existing_prd = backend.get_prd(stored_prd_id)

    if existing_prd is not None and (
        existing_prd.content_available
        and existing_prd.source_bytes == source.source_bytes
    ):
        return PrdPersistencePlan(
            action="unchanged",
            draft=None,
            revision=existing_prd.revision,
            source_sha256=existing_prd.source_sha256 or source.source_sha256,
            status=existing_prd.status.value,
        )

    if existing_prd is None:
        payload: dict[str, Any] = {
            "project_id": project_id,
            "expected_absent": True,
            "title": parsed.prd.title,
            "status": parsed.prd.status.value,
            "summary": parsed.prd.summary,
            "goals": parsed.prd.goals,
            "non_goals": parsed.prd.non_goals,
            "requirements": [
                requirement.model_dump(mode="json")
                for requirement in parsed.requirements
            ],
            "acceptance_criteria": parsed.prd.acceptance_criteria,
            "risks": parsed.prd.risks,
            "open_questions": parsed.prd.open_questions,
            "assumptions": [item.model_dump() for item in parsed.prd.assumptions],
            **source_binding(source, 1),
        }
        if not is_default:
            payload.update({
                "prd_id": stored_prd_id,
                "is_default": False,
                "target_version": parsed.prd.target_version,
                "target_tag": parsed.prd.target_tag,
            })
        return PrdPersistencePlan(
            action="parsed",
            draft=EventDraft(
                timestamp=clock.now(),
                actor=actor,
                action="prd.parsed",
                target_kind="prd",
                target_id=project_id,
                payload_json=payload,
            ),
            revision=1,
            source_sha256=source.source_sha256,
            status=parsed.prd.status.value,
        )

    new_requirements = list(parsed.requirements)
    live_requirements = backend.list_requirements(prd_id=stored_prd_id)
    live_by_id = {requirement.id: requirement for requirement in live_requirements}
    all_requirements = backend.list_requirements(
        prd_id=stored_prd_id,
        include_superseded=True,
    )
    all_ids = {requirement.id for requirement in all_requirements}
    new_by_id = {requirement.id: requirement for requirement in new_requirements}
    readded_retired = sorted(
        requirement_id
        for requirement_id in new_by_id
        if requirement_id in all_ids and requirement_id not in live_by_id
    )
    if readded_retired:
        from anvil.planning.diagnostics import format_identifier_summary

        ids = format_identifier_summary(readded_retired)
        raise PrdRevisionError(
            f"requirement id(s) {ids} were superseded in an earlier revision "
            "and cannot be re-added (ids are permanent lineage). Use a fresh id "
            "for the restored requirement."
        )

    revision = existing_prd.revision + 1
    payload = {
        "project_id": project_id,
        "prd_id": stored_prd_id,
        "revision": revision,
        "expected_status": existing_prd.status.value,
        "is_default": existing_prd.is_default,
        "title": parsed.prd.title,
        "target_version": existing_prd.target_version,
        "target_tag": existing_prd.target_tag,
        "status": existing_prd.status.value,
        "summary": parsed.prd.summary,
        "goals": parsed.prd.goals,
        "non_goals": parsed.prd.non_goals,
        "acceptance_criteria": parsed.prd.acceptance_criteria,
        "risks": parsed.prd.risks,
        "open_questions": parsed.prd.open_questions,
        "assumptions": [item.model_dump() for item in parsed.prd.assumptions],
        "requirements_added": [
            requirement.model_dump(mode="json")
            for requirement in new_requirements
            if requirement.id not in all_ids
        ],
        "requirements_superseded": [
            requirement.model_dump(mode="json")
            for requirement in live_requirements
            if requirement.id not in new_by_id
        ],
        "requirements_unchanged": [
            new_by_id[requirement_id].model_dump(mode="json")
            for requirement_id in live_by_id
            if requirement_id in new_by_id
        ],
        **source_binding(source, revision),
    }
    return PrdPersistencePlan(
        action="revised",
        draft=EventDraft(
            timestamp=clock.now(),
            actor=actor,
            action="prd.revised",
            target_kind="prd",
            target_id=stored_prd_id,
            payload_json=payload,
        ),
        revision=revision,
        source_sha256=source.source_sha256,
        status=existing_prd.status.value,
    )


__all__ = [
    "PROVIDER_PRD_CONTENT_OPERATION",
    "PROVIDER_READS_MERGE_COMMIT",
    "PrdPersistencePlan",
    "PrdRevisionError",
    "build_prd_persistence_plan",
    "source_binding",
]
