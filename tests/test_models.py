"""Tests for fakoli_state.state.models — Pydantic validation, enums, embedded objects.

Coverage targets:
- All 11 StrEnums
- All Pydantic model fields and validators
- Round-trip serialization for every major entity
- extra='forbid' enforcement
- UTC-aware datetime enforcement
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from fakoli_state.state.models import (
    PRD,
    Claim,
    ClaimStatus,
    ClaimType,
    ConflictGroup,
    Decision,
    Event,
    EventDraft,
    Evidence,
    Feature,
    FeatureStatus,
    PRDStatus,
    Project,
    Requirement,
    Review,
    ReviewDecision,
    ReviewTargetKind,
    Score,
    SyncMapping,
    SyncState,
    Task,
    TaskPriority,
    TaskStatus,
    TaskType,
    Verification,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = datetime.UTC
_NOW = datetime.datetime(2026, 5, 24, 18, 0, 0, tzinfo=_UTC)


def _make_project(**overrides: Any) -> Project:
    defaults: dict[str, Any] = {
        "id": "proj-1",
        "name": "Test Project",
        "description": "A test project",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return Project(**defaults)


def _make_task(**overrides: Any) -> Task:
    defaults: dict[str, Any] = {
        "id": "T001",
        "feature_id": "F001",
        "title": "Write tests",
        "description": "Comprehensive tests",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return Task(**defaults)


def _make_claim(**overrides: Any) -> Claim:
    defaults: dict[str, Any] = {
        "id": "C001",
        "task_id": "T001",
        "claimed_by": "agent-x",
        "created_at": _NOW,
        "lease_expires_at": _NOW + datetime.timedelta(hours=1),
        "last_heartbeat_at": _NOW,
    }
    defaults.update(overrides)
    return Claim(**defaults)


def _make_evidence(**overrides: Any) -> Evidence:
    defaults: dict[str, Any] = {
        "id": "EV001",
        "task_id": "T001",
        "claim_id": "C001",
        "submitted_at": _NOW,
        "submitted_by": "agent-x",
    }
    defaults.update(overrides)
    return Evidence(**defaults)


# ---------------------------------------------------------------------------
# Score validation
# ---------------------------------------------------------------------------


class TestScore:
    def test_score_all_none_by_default(self) -> None:
        """Unset Score has all scoring dimensions as None."""
        score = Score()
        assert score.complexity is None
        assert score.parallelizability is None
        assert score.context_load is None
        assert score.blast_radius is None
        assert score.review_risk is None
        assert score.agent_suitability is None
        assert score.explanation is None

    def test_score_valid_range(self) -> None:
        """Score accepts values 1 through 5."""
        score = Score(complexity=1, review_risk=5)
        assert score.complexity == 1
        assert score.review_risk == 5

    def test_score_dimensions_require_1_to_5_too_low(self) -> None:
        """Score with value 0 raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            Score(complexity=0)
        assert "complexity" in str(exc_info.value).lower() or "greater" in str(exc_info.value).lower()

    def test_score_dimensions_require_1_to_5_too_high(self) -> None:
        """Score with value 6 raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            Score(blast_radius=6)
        assert "blast_radius" in str(exc_info.value).lower() or "less" in str(exc_info.value).lower()

    def test_score_extra_fields_forbidden(self) -> None:
        """Score forbids extra fields."""
        with pytest.raises(ValidationError):
            Score.model_validate({"unknown_field": "bad"})

    def test_score_round_trip(self) -> None:
        """Score serializes and deserializes without loss."""
        original = Score(complexity=3, explanation="medium complexity")
        dumped = original.model_dump(mode="json")
        restored = Score.model_validate(dumped)
        assert restored == original


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TestVerification:
    def test_verification_defaults_empty_lists(self) -> None:
        """Verification fields default to empty lists."""
        v = Verification()
        assert v.commands == []
        assert v.manual_steps == []
        assert v.required_evidence == []

    def test_verification_round_trip(self) -> None:
        v = Verification(commands=["pytest -x"], required_evidence=["test output"])
        dumped = v.model_dump(mode="json")
        restored = Verification.model_validate(dumped)
        assert restored == v


# ---------------------------------------------------------------------------
# TaskStatus enum
# ---------------------------------------------------------------------------


class TestTaskStatusEnum:
    def test_task_status_enum_values_count(self) -> None:
        """All 11 task statuses are present."""
        expected = {
            "proposed",
            "drafted",
            "reviewed",
            "ready",
            "claimed",
            "in_progress",
            "blocked",
            "needs_review",
            "accepted",
            "done",
            "rejected",
        }
        actual = {s.value for s in TaskStatus}
        assert actual == expected

    def test_task_status_is_str(self) -> None:
        """TaskStatus values are strings (StrEnum)."""
        assert isinstance(TaskStatus.proposed, str)
        assert TaskStatus.proposed == "proposed"


# ---------------------------------------------------------------------------
# Datetime UTC enforcement
# ---------------------------------------------------------------------------


class TestDatetimeUTCEnforcement:
    def test_project_naive_created_at_raises(self) -> None:
        """Naive datetime in Project.created_at raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            Project(
                id="p1",
                name="Test",
                description="desc",
                created_at=datetime.datetime(2026, 1, 1),  # naive!
                updated_at=_NOW,
            )
        assert "timezone" in str(exc_info.value).lower() or "utc" in str(exc_info.value).lower() or "naive" in str(exc_info.value).lower()

    def test_project_naive_updated_at_raises(self) -> None:
        """Naive datetime in Project.updated_at raises ValidationError."""
        with pytest.raises(ValidationError):
            Project(
                id="p1",
                name="Test",
                description="desc",
                created_at=_NOW,
                updated_at=datetime.datetime(2026, 1, 1),  # naive!
            )

    def test_task_naive_created_at_raises(self) -> None:
        """Naive datetime in Task.created_at raises ValidationError."""
        with pytest.raises(ValidationError):
            _make_task(created_at=datetime.datetime(2026, 1, 1))

    def test_claim_naive_lease_expires_at_raises(self) -> None:
        """Naive datetime in Claim.lease_expires_at raises ValidationError."""
        with pytest.raises(ValidationError):
            _make_claim(lease_expires_at=datetime.datetime(2026, 1, 1))

    def test_evidence_naive_submitted_at_raises(self) -> None:
        """Naive datetime in Evidence.submitted_at raises ValidationError."""
        with pytest.raises(ValidationError):
            _make_evidence(submitted_at=datetime.datetime(2026, 1, 1))

    def test_prd_naive_last_reviewed_at_raises(self) -> None:
        """Naive datetime in PRD.last_reviewed_at raises ValidationError."""
        with pytest.raises(ValidationError):
            PRD(last_reviewed_at=datetime.datetime(2026, 1, 1))


# ---------------------------------------------------------------------------
# Task — default embedded objects
# ---------------------------------------------------------------------------


class TestTaskDefaults:
    def test_task_default_embedded_objects(self) -> None:
        """Task without scores/verification still has them as defaults."""
        task = _make_task()
        assert isinstance(task.scores, Score)
        assert task.scores.complexity is None
        assert isinstance(task.verification, Verification)
        assert task.verification.commands == []
        assert task.acceptance_criteria == []
        assert task.dependencies == []

    def test_task_default_status_proposed(self) -> None:
        """Task.status defaults to 'proposed'."""
        task = _make_task()
        assert task.status == TaskStatus.proposed

    def test_task_default_priority_medium(self) -> None:
        """Task.priority defaults to 'medium'."""
        task = _make_task()
        assert task.priority == TaskPriority.medium

    def test_task_default_task_type_feature(self) -> None:
        """Task.task_type defaults to 'feature' for backward compatibility."""
        task = _make_task()
        assert task.task_type == TaskType.feature


# ---------------------------------------------------------------------------
# TaskType enum (T015 — non-feature task types)
# ---------------------------------------------------------------------------


class TestTaskTypeEnum:
    def test_task_type_enum_values(self) -> None:
        """TaskType has exactly feature / bugfix / refactor / modify."""
        assert {t.value for t in TaskType} == {
            "feature",
            "bugfix",
            "refactor",
            "modify",
        }

    def test_task_type_is_str(self) -> None:
        """TaskType values are plain strings (StrEnum)."""
        assert isinstance(TaskType.bugfix, str)
        assert TaskType.bugfix == "bugfix"

    def test_task_type_accepts_non_feature_kinds(self) -> None:
        """A Task can be constructed with each non-feature task_type."""
        for kind in (TaskType.bugfix, TaskType.refactor, TaskType.modify):
            task = _make_task(task_type=kind)
            assert task.task_type == kind

    def test_task_type_round_trip_preserves_kind(self) -> None:
        """task_type survives a model_dump → model_validate round trip."""
        original = _make_task(task_type=TaskType.refactor)
        restored = Task.model_validate(original.model_dump(mode="json"))
        assert restored.task_type == TaskType.refactor
        assert restored == original

    def test_task_type_rejects_unknown_value(self) -> None:
        """An unknown task_type string is a ValidationError (extra='forbid' enum)."""
        with pytest.raises(ValidationError):
            _make_task(task_type="chore")

    def test_task_type_default_serializes_to_feature(self) -> None:
        """A task built without task_type serialises the 'feature' default."""
        dumped = _make_task().model_dump(mode="json")
        assert dumped["task_type"] == "feature"


# ---------------------------------------------------------------------------
# extra='forbid' enforcement
# ---------------------------------------------------------------------------


class TestExtraForbid:
    def test_project_extra_field_forbidden(self) -> None:
        """Passing unknown field to Project raises ValidationError."""
        with pytest.raises(ValidationError):
            Project(
                id="p1",
                name="Test",
                description="desc",
                created_at=_NOW,
                updated_at=_NOW,
                **{"unknown_extra": "bad"},
            )

    def test_task_extra_field_forbidden(self) -> None:
        """Passing unknown field to Task raises ValidationError."""
        with pytest.raises(ValidationError):
            _make_task(not_a_real_field="oops")

    def test_score_extra_field_forbidden(self) -> None:
        """Passing unknown field to Score raises ValidationError."""
        with pytest.raises(ValidationError):
            Score.model_validate({"extra_dim": 3})

    def test_verification_extra_field_forbidden(self) -> None:
        """Passing unknown field to Verification raises ValidationError."""
        with pytest.raises(ValidationError):
            Verification.model_validate({"unexpected": "bad"})


# ---------------------------------------------------------------------------
# Event ID format
# ---------------------------------------------------------------------------


class TestEventIdFormat:
    def test_event_id_format_valid(self) -> None:
        """Event(id='E000001') is accepted."""
        from fakoli_state.state.models import Event

        event = Event(
            id="E000001",
            timestamp=_NOW,
            actor="test",
            action="project.created",
            target_kind="project",
            target_id="p1",
        )
        assert event.id == "E000001"

    def test_event_id_format_invalid(self) -> None:
        """Event(id='garbage') raises ValidationError — id must start with E followed by digits."""
        from fakoli_state.state.models import Event

        with pytest.raises(ValidationError) as exc_info:
            Event(
                id="garbage",
                timestamp=_NOW,
                actor="test",
                action="project.created",
                target_kind="project",
                target_id="p1",
            )
        assert "E000001" in str(exc_info.value) or "monotonic" in str(exc_info.value)

    def test_event_id_format_must_have_digits(self) -> None:
        """Event id 'Eabc' (non-digits after E) also raises ValidationError."""
        from fakoli_state.state.models import Event

        with pytest.raises(ValidationError):
            Event(
                id="Eabc",
                timestamp=_NOW,
                actor="test",
                action="project.created",
                target_kind="project",
                target_id="p1",
            )


# ---------------------------------------------------------------------------
# EventDraft -> Event (SL1-RR-1 write-path types)
# ---------------------------------------------------------------------------


class TestEventDraft:
    def test_event_draft_has_no_id_field(self) -> None:
        """EventDraft carries every Event field except the backend-assigned ones."""
        assert "id" not in EventDraft.model_fields
        # Event adds exactly the backend-assigned envelope fields on top of
        # the draft: `id` (SL1-RR-1) plus the v1.22.0 git-mode chain fields
        # `parent_event_id` / `lamport` (None in local mode). All three are
        # assigned inside append()'s critical section, never by callers.
        assert set(Event.model_fields) == set(EventDraft.model_fields) | {
            "id",
            "parent_event_id",
            "lamport",
        }

    def test_event_is_subclass_of_draft(self) -> None:
        """Event extends EventDraft — the materialized form is-a draft."""
        assert issubclass(Event, EventDraft)

    def test_event_draft_forbids_id(self) -> None:
        """Passing `id` to a draft is rejected (extra='forbid')."""
        with pytest.raises(ValidationError):
            EventDraft(
                id="E000001",  # type: ignore[call-arg]
                timestamp=_NOW,
                actor="test",
                action="project.created",
                target_kind="project",
                target_id="p1",
            )

    def test_event_draft_requires_utc_timestamp(self) -> None:
        """A draft enforces the same UTC-aware timestamp rule as Event."""
        with pytest.raises(ValidationError):
            EventDraft(
                timestamp=datetime.datetime(2026, 1, 1),  # naive!
                actor="test",
                action="project.created",
                target_kind="project",
                target_id="p1",
            )

    def test_draft_to_event_round_trip(self) -> None:
        """An EventDraft promotes to an Event with an assigned id and the
        materialized Event serializes/validates without loss."""
        draft = EventDraft(
            timestamp=_NOW,
            actor="agent-x",
            action="task.applied",
            target_kind="task",
            target_id="T001",
            payload_json={"decision": "approve"},
        )

        # Promote the draft to a fact by assigning the backend-owned id.
        event = Event(id="E000007", **draft.model_dump())

        assert event.id == "E000007"
        assert event.timestamp == draft.timestamp
        assert event.actor == draft.actor
        assert event.action == draft.action
        assert event.target_kind == draft.target_kind
        assert event.target_id == draft.target_id
        assert event.payload_json == draft.payload_json

        # Round-trip the materialized Event through JSON.
        dumped = event.model_dump(mode="json")
        restored = Event.model_validate(dumped)
        assert restored == event


# ---------------------------------------------------------------------------
# Error signals (SL1-RR-1 write-path) — import + hierarchy
# ---------------------------------------------------------------------------


class TestWritePathErrors:
    def test_new_error_types_importable_and_in_hierarchy(self) -> None:
        """EventRejected and IdempotentNoOp live alongside the existing
        backend exceptions and share the BackendError base."""
        from fakoli_state.state.backend import (
            BackendError,
            EventRejected,
            IdempotentNoOp,
            StateLocked,
            TransactionAborted,
        )

        assert issubclass(EventRejected, BackendError)
        assert issubclass(IdempotentNoOp, BackendError)
        # The new signals are siblings of the existing ones, not the same type.
        assert EventRejected is not TransactionAborted
        assert IdempotentNoOp is not StateLocked

    def test_event_draft_exists_as_sole_write_type(self) -> None:
        """SL1-RR-1 T6: PENDING_EVENT_ID is retired; EventDraft is the write type.

        PENDING_EVENT_ID has been removed from backend.py. EventDraft (no id field)
        is now the sole type passed to append(). The id is assigned by the backend.
        """
        # EventDraft has no id field — the backend assigns it.

        from fakoli_state.state.models import EventDraft
        fields = EventDraft.model_fields
        assert "id" not in fields, "EventDraft must not have an id field"
        assert "action" in fields
        assert "timestamp" in fields


# ---------------------------------------------------------------------------
# Round-trip tests for major entities
# ---------------------------------------------------------------------------


class TestRoundTrips:
    def test_project_round_trip(self) -> None:
        original = _make_project()
        dumped = original.model_dump(mode="json")
        restored = Project.model_validate(dumped)
        assert restored == original

    def test_task_round_trip(self) -> None:
        original = _make_task(
            acceptance_criteria=["it works"],
            verification=Verification(commands=["pytest"]),
            scores=Score(complexity=3),
        )
        dumped = original.model_dump(mode="json")
        restored = Task.model_validate(dumped)
        assert restored == original

    def test_prd_round_trip(self) -> None:
        original = PRD(
            status=PRDStatus.reviewed,
            summary="test prd",
            goals=["goal 1"],
            last_reviewed_at=_NOW,
            last_reviewed_by="reviewer",
        )
        dumped = original.model_dump(mode="json")
        restored = PRD.model_validate(dumped)
        assert restored == original

    def test_claim_round_trip(self) -> None:
        original = _make_claim(branch="feat/test")
        dumped = original.model_dump(mode="json")
        restored = Claim.model_validate(dumped)
        assert restored == original

    def test_evidence_round_trip(self) -> None:
        original = _make_evidence(
            commands_run=["pytest -x"],
            files_changed=["src/foo.py"],
            pr_url="https://github.com/pr/1",
        )
        dumped = original.model_dump(mode="json")
        restored = Evidence.model_validate(dumped)
        assert restored == original

    def test_review_round_trip(self) -> None:
        original = Review(
            id="R001",
            target_kind=ReviewTargetKind.task,
            target_id="T001",
            reviewed_by="human",
            decision=ReviewDecision.approve,
            created_at=_NOW,
        )
        dumped = original.model_dump(mode="json")
        restored = Review.model_validate(dumped)
        assert restored == original

    def test_feature_round_trip(self) -> None:
        original = Feature(
            id="F001",
            title="Auth feature",
            description="Handles authentication",
            status=FeatureStatus.ready,
            tasks=["T001", "T002"],
        )
        dumped = original.model_dump(mode="json")
        restored = Feature.model_validate(dumped)
        assert restored == original

    def test_requirement_round_trip(self) -> None:
        original = Requirement(
            id="R001",
            prd_section="section-1",
            text="System must authenticate users",
            source_paragraph="para 1",
            derived=False,
        )
        dumped = original.model_dump(mode="json")
        restored = Requirement.model_validate(dumped)
        assert restored == original

    def test_decision_round_trip(self) -> None:
        original = Decision(
            id="D001",
            title="Use SQLite",
            context="Need embedded DB",
            decision="SQLite chosen",
            consequences="Single writer",
            created_at=_NOW,
            related_tasks=["T001"],
        )
        dumped = original.model_dump(mode="json")
        restored = Decision.model_validate(dumped)
        assert restored == original

    def test_conflict_group_round_trip(self) -> None:
        original = ConflictGroup(
            id="CG001",
            name="auth-overlap",
            task_ids=["T001", "T002"],
            reason="Both touch auth module",
        )
        dumped = original.model_dump(mode="json")
        restored = ConflictGroup.model_validate(dumped)
        assert restored == original

    def test_sync_mapping_round_trip(self) -> None:
        original = SyncMapping(
            task_id="T001",
            external_system="github_issues",
            external_id="gh-42",
            last_synced_at=_NOW,
            sync_state=SyncState.in_sync,
        )
        dumped = original.model_dump(mode="json")
        restored = SyncMapping.model_validate(dumped)
        assert restored == original


# ---------------------------------------------------------------------------
# Additional enum coverage
# ---------------------------------------------------------------------------


class TestEnumCoverage:
    def test_prd_status_values(self) -> None:
        assert set(PRDStatus) == {
            PRDStatus.draft,
            PRDStatus.reviewed,
            PRDStatus.approved,
            PRDStatus.rejected,
        }

    def test_feature_status_values(self) -> None:
        assert set(FeatureStatus) == {
            FeatureStatus.proposed,
            FeatureStatus.ready,
            FeatureStatus.in_progress,
            FeatureStatus.done,
        }

    def test_claim_type_values(self) -> None:
        values = {c.value for c in ClaimType}
        assert "task" in values
        assert "file_scope" in values

    def test_claim_status_values(self) -> None:
        assert ClaimStatus.active == "active"
        assert ClaimStatus.force_released == "force_released"

    def test_review_decision_values(self) -> None:
        assert set(ReviewDecision) == {
            ReviewDecision.approve,
            ReviewDecision.reject,
            ReviewDecision.needs_changes,
        }

    def test_sync_state_values(self) -> None:
        assert SyncState.conflict == "conflict"
        assert SyncState.external_deleted == "external_deleted"
