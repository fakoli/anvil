"""Canonical PRD source-to-event persistence planning.

This is the single producer used by the CLI, MCP, and planning batch paths.
It consumes the exact source bytes qualified by provider-read operation
``state.prd.content`` v1 (merged in provider-read-contracts at
``09914b40ffa56900cb47e6990fd62b5e42b212fc``) and binds every material change
to one revision/digest tuple.  Byte-identical persisted content is a no-op.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
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
    from anvil.state.models import PRD


PROVIDER_READS_MERGE_COMMIT = "09914b40ffa56900cb47e6990fd62b5e42b212fc"
PROVIDER_PRD_CONTENT_OPERATION = (
    PRD_CONTENT_OPERATION_ID,
    PRD_CONTENT_OPERATION_VERSION,
    PRD_CONTENT_SCHEMA_ID,
)
PRD_MATERIAL_DIGEST_DOMAIN = b"anvil.prd-material-content.v1\0"
PRD_TITLE_SENTINEL = "<ANVIL-PRD-TITLE-V1>"


class ExactPrdSource(Protocol):
    source_bytes: bytes
    markdown: str
    source_sha256: str
    source_size_bytes: int
    source_encoding: str


class PrdRevisionError(ValueError):
    """A deterministic source-to-revision refusal."""


class PrdClaimBindingError(ValueError):
    """The canonical source is not the exact approved lifecycle binding."""


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


def material_content_sha256(source: ExactPrdSource, title: str) -> str:
    """Hash exact source with only the successfully parsed H1 value replaced."""
    from anvil.planning.template import (
        _ATX_CLOSING_SEQUENCE_RE,
        _ATX_H1_RE,
        _root_atx_heading_lines,
    )

    markdown = source.markdown
    lines = markdown.splitlines(keepends=True)
    headings = _root_atx_heading_lines(markdown)
    h1_line = next((line for line, level in headings.items() if level == 1), None)
    if h1_line is None or h1_line > len(lines):
        raise PrdRevisionError("parsed PRD has no exact H1 source binding")
    raw_with_ending = lines[h1_line - 1]
    raw = raw_with_ending.rstrip("\r\n")
    match = _ATX_H1_RE.fullmatch(raw)
    if match is None or match.group("heading") is None:
        raise PrdRevisionError("parsed PRD H1 source binding is malformed")
    heading = match.group("heading")
    heading_start = match.start("heading")
    left = len(heading) - len(heading.lstrip(" \t"))
    right = len(heading.rstrip(" \t"))
    core = heading[left:right]
    closing = _ATX_CLOSING_SEQUENCE_RE.search(core)
    if closing is not None and closing.end() == len(core):
        core = core[: closing.start()].rstrip(" \t")
    prefix = re.match(r"^Project:[ \t]*", core, flags=re.IGNORECASE)
    value = core[prefix.end() :] if prefix is not None else core
    value_leading = len(value) - len(value.lstrip(" \t"))
    value = value.strip(" \t")
    if value != title:
        raise PrdRevisionError("parsed PRD title does not match its exact H1 source")
    title_start_in_heading = left + (prefix.end() if prefix is not None else 0)
    title_start_in_heading += value_leading
    title_start_in_line = heading_start + title_start_in_heading
    prefix_chars = sum(len(line) for line in lines[: h1_line - 1])
    title_start = prefix_chars + title_start_in_line
    title_end = title_start + len(title)
    material = (
        markdown[:title_start] + PRD_TITLE_SENTINEL + markdown[title_end:]
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(PRD_MATERIAL_DIGEST_DOMAIN + material).hexdigest()


def require_canonical_prd_claim_binding(state_dir: Path, prd: PRD | None) -> None:
    """Refuse a new claim unless canonical source is exactly approved.

    Existing claims never call this boundary, so renew/progress/submit/apply
    remain available while a changed source is being reparsed and reviewed.
    """
    from anvil.cli._helpers import (
        PrdSourceIngestError,
        ingest_prd_source_for_id,
    )

    if prd is None:
        raise PrdClaimBindingError(
            "owning PRD is unavailable; parse, review, and approve it before claiming"
        )
    exact_binding = (
        prd.revision,
        prd.source_sha256,
        prd.material_sha256,
        prd.content_event_id,
    )
    lifecycle_binding = (
        prd.lifecycle_revision,
        prd.lifecycle_source_sha256,
        prd.lifecycle_material_sha256,
        prd.lifecycle_content_event_id,
    )
    if (
        prd.status.value != "approved"
        or not prd.content_available
        or any(value is None for value in exact_binding[1:])
        or lifecycle_binding != exact_binding
        or prd.review_event_id is None
    ):
        raise PrdClaimBindingError(
            "owning PRD is not bound to an exact approved canonical revision; "
            "parse, review, and approve it before claiming"
        )
    try:
        source = ingest_prd_source_for_id(state_dir, prd.id)
    except PrdSourceIngestError:
        raise PrdClaimBindingError(
            "canonical PRD source cannot be verified; parse, review, and approve "
            "the managed source before claiming"
        ) from None
    if (
        source.source_bytes != prd.source_bytes
        or source.source_sha256 != prd.source_sha256
    ):
        raise PrdClaimBindingError(
            "canonical PRD source changed after approval; parse, review, and approve "
            "it before claiming"
        )


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
    material_sha256 = material_content_sha256(source, parsed.prd.title)

    if existing_prd is not None and (
        existing_prd.content_available
        and existing_prd.source_bytes == source.source_bytes
        and existing_prd.title == parsed.prd.title
        and existing_prd.material_sha256 == material_sha256
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
            "material_sha256": material_sha256,
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
    exact_parent = (
        existing_prd.content_available
        and existing_prd.source_sha256 is not None
        and existing_prd.material_sha256 is not None
        and existing_prd.content_event_id is not None
    )
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
        "material_sha256": material_sha256,
        **source_binding(source, revision),
    }
    if exact_parent:
        payload.update({
            "lineage_version": 1,
            "parent_revision": existing_prd.revision,
            "parent_source_sha256": existing_prd.source_sha256,
            "parent_material_sha256": existing_prd.material_sha256,
            "parent_content_event_id": existing_prd.content_event_id,
            "expected_lifecycle_revision": existing_prd.lifecycle_revision,
            "expected_lifecycle_source_sha256": (
                existing_prd.lifecycle_source_sha256
            ),
            "expected_lifecycle_material_sha256": (
                existing_prd.lifecycle_material_sha256
            ),
            "expected_lifecycle_content_event_id": (
                existing_prd.lifecycle_content_event_id
            ),
        })
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
    "PRD_MATERIAL_DIGEST_DOMAIN",
    "PRD_TITLE_SENTINEL",
    "PrdPersistencePlan",
    "PrdClaimBindingError",
    "PrdRevisionError",
    "build_prd_persistence_plan",
    "material_content_sha256",
    "require_canonical_prd_claim_binding",
    "source_binding",
]
