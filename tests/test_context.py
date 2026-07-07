"""Tests for anvil.context.packets — work-packet renderer.

Coverage targets (>= 85%):
- render_packet() happy paths (minimal and full inputs)
- WorkPacket fields
- Markdown section presence/absence
- JSON output structure and ISO datetimes
- Determinism guarantee
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from anvil.config import Config
from anvil.context.packets import (
    FAST_LANE_REQUIRED_EVIDENCE_MAX,
    WorkPacket,
    fast_lane_packet,
    is_lightweight,
    render_packet,
)
from anvil.state.models import (
    Claim,
    ClaimStatus,
    ClaimType,
    Decision,
    Feature,
    FeatureStatus,
    Score,
    Task,
    TaskPriority,
    TaskStatus,
    TaskType,
    Verification,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = UTC
_T0 = datetime(2026, 5, 24, 18, 0, 0, tzinfo=_UTC)


def _make_task(
    *,
    task_id: str = "T001",
    feature_id: str = "F001",
    title: str = "Implement the thing",
    description: str = "Build the feature end-to-end.",
    status: TaskStatus = TaskStatus.ready,
    priority: TaskPriority = TaskPriority.medium,
    task_type: TaskType = TaskType.feature,
    acceptance_criteria: list[str] | None = None,
    implementation_notes: list[str] | None = None,
    verification_commands: list[str] | None = None,
    required_evidence: list[str] | None = None,
    likely_files: list[str] | None = None,
    scores: Score | None = None,
    now: datetime = _T0,
) -> Task:
    return Task(
        id=task_id,
        feature_id=feature_id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        task_type=task_type,
        acceptance_criteria=acceptance_criteria or ["Tests pass.", "Docs updated."],
        implementation_notes=implementation_notes or [],
        verification=Verification(
            commands=verification_commands or ["pytest tests/ -v"],
            manual_steps=[],
            required_evidence=required_evidence or [],
        ),
        likely_files=likely_files or [],
        scores=scores or Score(),
        created_at=now,
        updated_at=now,
    )


def _make_feature(
    *,
    feat_id: str = "F001",
    title: str = "User Authentication",
) -> Feature:
    return Feature(
        id=feat_id,
        title=title,
        description="Allow users to sign in.",
        status=FeatureStatus.in_progress,
        requirements=["R001"],
        tasks=["T001"],
    )


def _make_decision(
    *,
    dec_id: str = "D001",
    title: str = "Use JWT for sessions",
    related_tasks: list[str] | None = None,
    now: datetime = _T0,
) -> Decision:
    return Decision(
        id=dec_id,
        title=title,
        context="We need stateless auth.",
        decision="Use JWT with short expiry.",
        consequences="Clients must refresh tokens.",
        created_at=now,
        related_tasks=related_tasks or ["T001"],
    )


def _make_claim(
    *,
    claim_id: str = "C001",
    task_id: str = "T001",
    actor: str = "agent-alpha",
    now: datetime = _T0,
) -> Claim:
    return Claim(
        id=claim_id,
        task_id=task_id,
        claimed_by=actor,
        claim_type=ClaimType.task,
        status=ClaimStatus.active,
        branch="feat/t001-implement-the-thing",
        worktree_path=None,
        expected_files=["src/auth.py"],
        created_at=now,
        lease_expires_at=now + timedelta(hours=1),
        last_heartbeat_at=now,
    )


# ===========================================================================
# TestRenderPacket
# ===========================================================================


class TestRenderPacket:
    def test_minimal_task_renders(self) -> None:
        """Task with no feature, no deps, no decisions, no claim → packet with
        title, status, acceptance criteria, and verification commands.
        """
        task = _make_task()
        packet = render_packet(task)

        assert isinstance(packet, WorkPacket)
        md = packet.markdown

        # Header
        assert "T001" in md
        assert "Implement the thing" in md

        # Status present
        assert "ready" in md.lower()

        # Acceptance criteria
        assert "Acceptance criteria" in md
        assert "Tests pass." in md
        assert "Docs updated." in md

        # Verification commands
        assert "Verification" in md
        assert "pytest tests/ -v" in md

    def test_full_task_with_feature_and_deps(self) -> None:
        """Task + Feature + 1 completed dep + 1 open dep + 1 decision + active claim
        → markdown includes all sections in the documented order.
        """
        task = _make_task(
            task_id="T002",
            implementation_notes=["Do not break auth flow."],
        )
        feature = _make_feature()
        dep_done = _make_task(
            task_id="T001",
            title="Setup DB schema",
            status=TaskStatus.done,
        )
        dep_open = _make_task(
            task_id="T003",
            title="Design API contract",
            status=TaskStatus.ready,
        )
        decision = _make_decision(related_tasks=["T002"])
        claim = _make_claim(task_id="T002")

        packet = render_packet(
            task,
            feature=feature,
            dependencies_completed=[dep_done],
            dependencies_open=[dep_open],
            related_decisions=[decision],
            active_claim=claim,
        )

        md = packet.markdown

        # Feature section in header
        assert "F001" in md
        assert "User Authentication" in md

        # Dependencies (completed)
        assert "Dependencies (completed)" in md
        assert "T001" in md
        assert "Setup DB schema" in md

        # Dependencies (open)
        assert "Dependencies (open)" in md
        assert "T003" in md
        assert "Design API contract" in md

        # Decisions
        assert "Decisions affecting this task" in md
        assert "D001" in md
        assert "Use JWT for sessions" in md

        # Constraints
        assert "Constraints" in md
        assert "Do not break auth flow." in md

        # Active claim
        assert "Active claim" in md
        assert "C001" in md

        # Update protocol mentions renew
        assert "renew" in md.lower()

    def test_unscored_task_renders_unscored(self) -> None:
        """Task with all Score dimensions None → markdown shows 'unscored'."""
        task = _make_task(scores=Score())  # all None by default
        packet = render_packet(task)
        md = packet.markdown
        assert "unscored" in md

    def test_implementation_notes_empty_renders_none_declared(self) -> None:
        """When implementation_notes is empty, Constraints section says 'None declared.'"""
        task = _make_task(implementation_notes=[])
        packet = render_packet(task)
        md = packet.markdown
        assert "None declared." in md

    def test_active_claim_section_present_when_claim_passed(self) -> None:
        """When active_claim is provided, 'Active claim' section appears in markdown."""
        task = _make_task()
        claim = _make_claim()
        packet = render_packet(task, active_claim=claim)
        assert "Active claim" in packet.markdown
        assert "C001" in packet.markdown

    def test_active_claim_section_absent_when_no_claim(self) -> None:
        """When no active_claim, 'Active claim' section does not appear in markdown."""
        task = _make_task()
        packet = render_packet(task)
        assert "Active claim" not in packet.markdown

    def test_packet_id_field(self) -> None:
        """WorkPacket.task_id equals task.id."""
        task = _make_task(task_id="T042")
        packet = render_packet(task)
        assert packet.task_id == "T042"

    def test_packet_markdown_is_deterministic(self) -> None:
        """Same input twice → identical markdown (no time-based or random elements)."""
        task = _make_task()
        feature = _make_feature()
        decision = _make_decision()
        claim = _make_claim()

        packet_a = render_packet(
            task,
            feature=feature,
            related_decisions=[decision],
            active_claim=claim,
        )
        packet_b = render_packet(
            task,
            feature=feature,
            related_decisions=[decision],
            active_claim=claim,
        )

        assert packet_a.markdown == packet_b.markdown

    def test_scored_task_shows_numeric_score(self) -> None:
        """When scores are set, numeric values appear in the header."""
        score = Score(
            complexity=3,
            agent_suitability=4,
            parallelizability=2,
            context_load=1,
            blast_radius=2,
            review_risk=3,
        )
        task = _make_task(scores=score)
        packet = render_packet(task)
        md = packet.markdown
        assert "3/5" in md
        assert "4/5" in md

    def test_likely_files_section_appears_when_set(self) -> None:
        """When likely_files is non-empty, 'Scope (likely files)' section appears."""
        task = _make_task(likely_files=["src/auth.py", "tests/test_auth.py"])
        packet = render_packet(task)
        md = packet.markdown
        assert "Scope" in md
        assert "src/auth.py" in md
        assert "tests/test_auth.py" in md

    def test_likely_files_section_absent_when_empty(self) -> None:
        """When likely_files is empty, 'Scope' section does not appear."""
        task = _make_task(likely_files=[])
        packet = render_packet(task)
        assert "Scope" not in packet.markdown

    def test_no_deps_sections_absent(self) -> None:
        """When no dependencies, neither 'Dependencies (completed)' nor
        'Dependencies (open)' appear.
        """
        task = _make_task()
        packet = render_packet(task)
        assert "Dependencies (completed)" not in packet.markdown
        assert "Dependencies (open)" not in packet.markdown

    def test_no_decisions_section_absent(self) -> None:
        """When no decisions, 'Decisions affecting this task' does not appear."""
        task = _make_task()
        packet = render_packet(task)
        assert "Decisions affecting this task" not in packet.markdown

    def test_update_protocol_always_present(self) -> None:
        """'Update protocol' section always appears in markdown."""
        task = _make_task()
        packet = render_packet(task)
        assert "Update protocol" in packet.markdown

    def test_constraints_section_always_present(self) -> None:
        """'Constraints / non-goals' section always appears, even when empty."""
        task = _make_task(implementation_notes=[])
        packet = render_packet(task)
        assert "Constraints" in packet.markdown

    def test_markdown_ends_with_newline(self) -> None:
        """Markdown output ends with exactly one newline."""
        task = _make_task()
        packet = render_packet(task)
        assert packet.markdown.endswith("\n")
        assert not packet.markdown.endswith("\n\n")


# ===========================================================================
# TestRenderPacketJSON
# ===========================================================================


class TestRenderPacketJSON:
    def test_json_format_uses_iso_datetimes(self) -> None:
        """task.created_at appears in JSON output as ISO 8601 string."""
        task = _make_task()
        packet = render_packet(task)
        json_data = packet.json_data

        task_json: dict[str, Any] = json_data["task"]
        created_at_str = task_json.get("created_at")

        assert isinstance(created_at_str, str), (
            f"created_at should be a string in json_data, got {type(created_at_str)!r}"
        )
        # Must be parseable as ISO 8601
        parsed = datetime.fromisoformat(created_at_str)
        assert parsed == _T0, f"Parsed datetime {parsed!r} != {_T0!r}"

    def test_json_format_mirrors_markdown_sections(self) -> None:
        """json_data has keys for task, feature, dependencies_completed,
        dependencies_open, decisions, active_claim, update_protocol.
        """
        task = _make_task()
        feature = _make_feature()
        dep_done = _make_task(task_id="T000", status=TaskStatus.done)
        dep_open = _make_task(task_id="T002", status=TaskStatus.ready)
        decision = _make_decision()
        claim = _make_claim()

        packet = render_packet(
            task,
            feature=feature,
            dependencies_completed=[dep_done],
            dependencies_open=[dep_open],
            related_decisions=[decision],
            active_claim=claim,
        )

        jd = packet.json_data
        assert "task_id" in jd
        assert "task" in jd
        assert "feature" in jd
        assert "dependencies_completed" in jd
        assert "dependencies_open" in jd
        assert "decisions" in jd
        assert "active_claim" in jd
        assert "update_protocol" in jd

        # Correct types
        assert isinstance(jd["task"], dict)
        assert isinstance(jd["feature"], dict)
        assert isinstance(jd["dependencies_completed"], list)
        assert isinstance(jd["dependencies_open"], list)
        assert isinstance(jd["decisions"], list)
        assert isinstance(jd["active_claim"], dict)
        assert isinstance(jd["update_protocol"], dict)

    def test_json_format_omits_none_fields(self) -> None:
        """When feature is absent, json_data['feature'] is None (not a dict)."""
        task = _make_task()
        packet = render_packet(task)
        jd = packet.json_data
        # feature absent → None
        assert jd.get("feature") is None

    def test_json_active_claim_none_when_no_claim(self) -> None:
        """When no claim, json_data['active_claim'] is None."""
        task = _make_task()
        packet = render_packet(task)
        assert packet.json_data["active_claim"] is None

    def test_json_task_id_matches_packet_task_id(self) -> None:
        """json_data['task_id'] == WorkPacket.task_id == task.id."""
        task = _make_task(task_id="T999")
        packet = render_packet(task)
        assert packet.json_data["task_id"] == "T999"
        assert packet.task_id == "T999"

    def test_json_update_protocol_has_submit_command(self) -> None:
        """update_protocol in json_data contains a 'submit_command' key."""
        task = _make_task(task_id="T005")
        packet = render_packet(task)
        up = packet.json_data["update_protocol"]
        assert "submit_command" in up
        assert "T005" in up["submit_command"]

    def test_json_update_protocol_has_renew_command_when_claim_present(self) -> None:
        """With an active claim, update_protocol includes 'renew_command'."""
        task = _make_task()
        claim = _make_claim()
        packet = render_packet(task, active_claim=claim)
        up = packet.json_data["update_protocol"]
        assert "renew_command" in up
        assert "C001" in up["renew_command"]

    def test_json_update_protocol_no_renew_command_when_no_claim(self) -> None:
        """Without a claim, update_protocol does not have 'renew_command'."""
        task = _make_task()
        packet = render_packet(task)
        up = packet.json_data["update_protocol"]
        assert "renew_command" not in up

    def test_json_decisions_list_correct_length(self) -> None:
        """json_data['decisions'] has one entry per decision passed in."""
        task = _make_task()
        d1 = _make_decision(dec_id="D001")
        d2 = _make_decision(dec_id="D002", title="Use Postgres")
        packet = render_packet(task, related_decisions=[d1, d2])
        assert len(packet.json_data["decisions"]) == 2

    def test_json_deps_completed_and_open_correct_lengths(self) -> None:
        """json_data lists for completed/open deps have correct lengths."""
        task = _make_task()
        done1 = _make_task(task_id="T001", status=TaskStatus.done)
        done2 = _make_task(task_id="T002", status=TaskStatus.done)
        open1 = _make_task(task_id="T003", status=TaskStatus.ready)

        packet = render_packet(
            task,
            dependencies_completed=[done1, done2],
            dependencies_open=[open1],
        )

        assert len(packet.json_data["dependencies_completed"]) == 2
        assert len(packet.json_data["dependencies_open"]) == 1

    def test_json_is_json_serializable(self) -> None:
        """json_data can be serialised with json.dumps without error."""
        task = _make_task()
        feature = _make_feature()
        claim = _make_claim()
        packet = render_packet(task, feature=feature, active_claim=claim)
        # Should not raise
        serialised = json.dumps(packet.json_data)
        assert serialised  # non-empty


# ---------------------------------------------------------------------------
# T015 — task_type in the packet + lightweight-variant routing by score
# ---------------------------------------------------------------------------


_LIGHT_SCORE = Score(
    complexity=1,
    parallelizability=4,
    context_load=2,
    blast_radius=1,
    review_risk=2,
    agent_suitability=5,
)
_HEAVY_SCORE = Score(
    complexity=5,
    parallelizability=2,
    context_load=4,
    blast_radius=5,
    review_risk=5,
    agent_suitability=1,
)


class TestTaskTypeInPacket:
    def test_packet_header_shows_task_type(self) -> None:
        """The work packet markdown renders the task's **Type:** line."""
        task = _make_task(task_type=TaskType.bugfix)
        packet = render_packet(task)
        assert "**Type:** bugfix" in packet.markdown

    def test_packet_json_carries_task_type(self) -> None:
        """json_data['task']['task_type'] mirrors the model field."""
        task = _make_task(task_type=TaskType.refactor)
        packet = render_packet(task)
        assert packet.json_data["task"]["task_type"] == "refactor"


class TestLightweightPacketRouting:
    def test_is_lightweight_true_for_low_complexity_low_blast(self) -> None:
        task = _make_task(scores=_LIGHT_SCORE)
        assert is_lightweight(task) is True

    def test_is_lightweight_false_for_high_blast(self) -> None:
        task = _make_task(scores=_HEAVY_SCORE)
        assert is_lightweight(task) is False

    def test_is_lightweight_false_when_unscored(self) -> None:
        """An unscored task always takes the full packet (never trim blind)."""
        task = _make_task(scores=Score())
        assert is_lightweight(task) is False

    def test_low_score_task_renders_lightweight_variant(self) -> None:
        """A small low-blast task routes to the lightweight packet."""
        task = _make_task(scores=_LIGHT_SCORE)
        packet = render_packet(task)
        assert packet.variant == "lightweight"
        assert packet.json_data["variant"] == "lightweight"
        # Lightweight trims the status-flow walkthrough.
        assert "Status will transition" not in packet.markdown
        assert "status_flow" not in packet.json_data["update_protocol"]
        # But the load-bearing submit instruction is always present.
        assert "anvil submit" in packet.markdown

    def test_high_blast_task_renders_full_variant(self) -> None:
        """A high-blast task keeps the full packet even at low complexity."""
        task = _make_task(scores=_HEAVY_SCORE)
        packet = render_packet(task)
        assert packet.variant == "full"
        assert packet.json_data["variant"] == "full"
        assert "Status will transition" in packet.markdown
        assert "status_flow" in packet.json_data["update_protocol"]

    def test_high_blast_refactor_is_not_lightweight(self) -> None:
        """task_type is irrelevant to routing — a high-blast refactor is full."""
        task = _make_task(task_type=TaskType.refactor, scores=_HEAVY_SCORE)
        packet = render_packet(task)
        assert packet.variant == "full"

    def test_lightweight_override_forces_variant(self) -> None:
        """An explicit lightweight=False keeps a low-score task on the full packet."""
        task = _make_task(scores=_LIGHT_SCORE)
        packet = render_packet(task, lightweight=False)
        assert packet.variant == "full"
        assert "Status will transition" in packet.markdown

    def test_lightweight_and_full_share_load_bearing_sections(self) -> None:
        """Both variants always carry goal + acceptance + verification + scope."""
        light = render_packet(
            _make_task(scores=_LIGHT_SCORE, likely_files=["src/a.py"])
        )
        full = render_packet(
            _make_task(scores=_HEAVY_SCORE, likely_files=["src/a.py"])
        )
        for packet in (light, full):
            assert "## Goal" in packet.markdown
            assert "## Acceptance criteria" in packet.markdown
            assert "## Verification" in packet.markdown
            assert "## Scope (likely files)" in packet.markdown


# ---------------------------------------------------------------------------
# T020 — fast-lane work packets for trivial-scored tasks
#
# Acceptance criteria:
#  - Tasks scoring below configurable complexity/blast thresholds render a
#    minimal packet (fewer required evidence fields, single-step) while still
#    recording a completion-evidence transition.
#  - The threshold is config-driven.
#  - A trivial-scored task completes via the fast-lane and still produces an
#    immutable evidence record; a high-blast task still requires the full packet.
# ---------------------------------------------------------------------------

_THREE_EVIDENCE = ["test output", "PR link", "screenshots"]


def _make_config(
    *,
    fast_lane_complexity_max: int = 2,
    fast_lane_blast_radius_max: int = 2,
) -> Config:
    """A minimal Config carrying just the T020 fast-lane ceilings."""
    return Config(
        project_name="Test Project",
        project_id="test-id",
        fast_lane_complexity_max=fast_lane_complexity_max,
        fast_lane_blast_radius_max=fast_lane_blast_radius_max,
    )


class TestFastLaneEvidenceTrim:
    """The fast-lane packet shows fewer required-evidence fields (single-step)."""

    def test_fast_lane_packet_trims_required_evidence_to_single_step(self) -> None:
        """A trivial-scored task renders only the first required-evidence item."""
        task = _make_task(scores=_LIGHT_SCORE, required_evidence=_THREE_EVIDENCE)
        packet = render_packet(task)
        assert packet.variant == "lightweight"
        # Markdown shows the first (essential) item but not the trailing ones.
        assert "- test output" in packet.markdown
        assert "- PR link" not in packet.markdown
        assert "- screenshots" not in packet.markdown
        # And it advertises the trim so nothing looks silently dropped.
        assert "fast-lane" in packet.markdown.lower()

    def test_fast_lane_json_carries_trimmed_required_evidence(self) -> None:
        """json_data['required_evidence'] mirrors the markdown trim."""
        task = _make_task(scores=_LIGHT_SCORE, required_evidence=_THREE_EVIDENCE)
        packet = render_packet(task)
        assert packet.json_data["required_evidence"] == ["test output"]
        assert (
            len(packet.json_data["required_evidence"])
            == FAST_LANE_REQUIRED_EVIDENCE_MAX
        )

    def test_fast_lane_preserves_full_evidence_in_task_record(self) -> None:
        """The trim is render-only: the immutable task record keeps every item."""
        task = _make_task(scores=_LIGHT_SCORE, required_evidence=_THREE_EVIDENCE)
        packet = render_packet(task)
        # The full ledger lives on task.verification untouched...
        assert list(task.verification.required_evidence) == _THREE_EVIDENCE
        # ...and the packet's structured task copy still carries all three.
        assert (
            packet.json_data["task"]["verification"]["required_evidence"]
            == _THREE_EVIDENCE
        )

    def test_high_blast_task_keeps_full_required_evidence(self) -> None:
        """A high-blast task still requires the FULL evidence checklist."""
        task = _make_task(scores=_HEAVY_SCORE, required_evidence=_THREE_EVIDENCE)
        packet = render_packet(task)
        assert packet.variant == "full"
        for item in _THREE_EVIDENCE:
            assert f"- {item}" in packet.markdown
        assert packet.json_data["required_evidence"] == _THREE_EVIDENCE
        assert "fast-lane" not in packet.markdown.lower()

    def test_fast_lane_with_single_evidence_item_adds_no_trim_note(self) -> None:
        """When the declared list is already minimal, no trim note is emitted."""
        task = _make_task(scores=_LIGHT_SCORE, required_evidence=["test output"])
        packet = render_packet(task)
        assert "- test output" in packet.markdown
        assert "fast-lane" not in packet.markdown.lower()

    def test_fast_lane_evidence_trim_disabled_by_negative_max(self) -> None:
        """A negative ceiling renders the full evidence list even on the fast-lane."""
        task = _make_task(scores=_LIGHT_SCORE, required_evidence=_THREE_EVIDENCE)
        packet = render_packet(task, fast_lane_required_evidence_max=-1)
        assert packet.variant == "lightweight"
        assert packet.json_data["required_evidence"] == _THREE_EVIDENCE


class TestFastLaneConfigDrivenThresholds:
    """The fast-lane routing reads its ceilings from config (config-driven)."""

    def test_fast_lane_packet_routes_lightweight_at_default_ceilings(self) -> None:
        """A 1/1 task routes to the fast-lane under the default 2/2 ceilings."""
        task = _make_task(scores=_LIGHT_SCORE, required_evidence=_THREE_EVIDENCE)
        packet = fast_lane_packet(task, _make_config())
        assert packet.variant == "lightweight"
        assert packet.json_data["required_evidence"] == ["test output"]

    def test_fast_lane_config_lowering_ceiling_excludes_borderline_task(self) -> None:
        """Lowering the complexity ceiling forces a borderline task to full."""
        # complexity=2 task: fast-lane under the default ceiling (2)...
        borderline = Score(
            complexity=2,
            parallelizability=4,
            context_load=2,
            blast_radius=1,
            review_risk=2,
            agent_suitability=4,
        )
        task = _make_task(scores=borderline, required_evidence=_THREE_EVIDENCE)
        assert fast_lane_packet(task, _make_config()).variant == "lightweight"
        # ...but full once config lowers the complexity ceiling to 1.
        tightened = _make_config(fast_lane_complexity_max=1)
        full = fast_lane_packet(task, tightened)
        assert full.variant == "full"
        assert full.json_data["required_evidence"] == _THREE_EVIDENCE

    def test_fast_lane_config_raising_ceiling_includes_larger_task(self) -> None:
        """Raising the blast ceiling pulls a mid-blast task into the fast-lane."""
        mid = Score(
            complexity=2,
            parallelizability=3,
            context_load=2,
            blast_radius=4,
            review_risk=2,
            agent_suitability=3,
        )
        task = _make_task(scores=mid, required_evidence=_THREE_EVIDENCE)
        # Default 2/2 ceilings: blast_radius=4 is above the ceiling → full.
        assert fast_lane_packet(task, _make_config()).variant == "full"
        # Raise the blast ceiling to 4 → now fast-lane.
        widened = _make_config(fast_lane_blast_radius_max=4)
        assert fast_lane_packet(task, widened).variant == "lightweight"

    def test_fast_lane_high_blast_never_fast_laned_at_default_config(self) -> None:
        """A 5/5-blast task stays on the full packet under default config."""
        task = _make_task(scores=_HEAVY_SCORE, required_evidence=_THREE_EVIDENCE)
        packet = fast_lane_packet(task, _make_config())
        assert packet.variant == "full"
        assert packet.json_data["required_evidence"] == _THREE_EVIDENCE

    def test_fast_lane_unscored_task_takes_full_packet(self) -> None:
        """An unscored task is never fast-laned regardless of config."""
        task = _make_task(scores=Score(), required_evidence=_THREE_EVIDENCE)
        packet = fast_lane_packet(task, _make_config())
        assert packet.variant == "full"


# ---------------------------------------------------------------------------
# retro-opps T002 — review tier in the packet (markdown + JSON)
# ---------------------------------------------------------------------------


_CONFIRMED_LIGHT_SCORE = Score(
    complexity=1,
    parallelizability=4,
    context_load=2,
    blast_radius=1,
    review_risk=2,
    agent_suitability=5,
    blast_radius_confirmed=True,
    review_risk_confirmed=True,
)


class TestReviewTierInPacket:
    def test_json_carries_review_tier_for_scored_and_unscored(self) -> None:
        """AC: packet JSON includes review_tier for scored AND unscored tasks."""
        scored = render_packet(_make_task(scores=_LIGHT_SCORE))
        unscored = render_packet(_make_task(scores=Score()))
        assert scored.json_data["review_tier"] == "standard"  # unconfirmed
        assert unscored.json_data["review_tier"] == "max"

    def test_markdown_max_tier_carries_max_guidance(self) -> None:
        packet = render_packet(_make_task(scores=_HEAVY_SCORE))
        assert "**Review tier:** max" in packet.markdown
        assert "adversarial review pass" in packet.markdown

    def test_markdown_light_tier_carries_light_guidance(self) -> None:
        packet = render_packet(_make_task(scores=_CONFIRMED_LIGHT_SCORE))
        assert "**Review tier:** light" in packet.markdown
        assert "evidence gate only" in packet.markdown

    def test_explicit_override_wins_over_derivation(self) -> None:
        """Mirrors the lightweight override: caller-supplied tier is rendered."""
        packet = render_packet(
            _make_task(scores=_HEAVY_SCORE), review_tier="standard"
        )
        assert packet.json_data["review_tier"] == "standard"
        assert "**Review tier:** standard" in packet.markdown

    def test_fast_lane_packet_derives_tier_from_config(self) -> None:
        """Config-aware path: same thresholds drive routing AND tier."""
        task = _make_task(scores=_CONFIRMED_LIGHT_SCORE)
        packet = fast_lane_packet(task, _make_config())
        assert packet.variant == "lightweight"
        assert packet.json_data["review_tier"] == "light"

    def test_fast_lane_variant_and_tier_can_diverge_when_unconfirmed(self) -> None:
        """Packet size and review depth are separable: fast-lane packet,
        standard review when risk flags are unconfirmed."""
        task = _make_task(scores=_LIGHT_SCORE)
        packet = fast_lane_packet(task, _make_config())
        assert packet.variant == "lightweight"
        assert packet.json_data["review_tier"] == "standard"
