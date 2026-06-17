"""Integration tests for Phase 7 Wave 2 — LLM augmentation of planning engine.

Wave 1 produced ``fakoli_state.planning.llm`` (Protocol + AnthropicProvider +
RecordedLLMProvider).  Wave 2 wires that into:

- :func:`fakoli_state.planning.scoring.score_task` (explanation enrichment)
- :func:`fakoli_state.planning.template.parse_prd`  (short description enrichment)
- :func:`fakoli_state.planning.inference.expand_task` (sub-task proposals)

These tests use the deterministic :class:`RecordedLLMProvider` exclusively —
no live API calls, no SDK mocking.  Each test pre-computes the lookup key
with :meth:`RecordedLLMProvider.record_key`, registers the canned response,
and verifies that the LLM-enriched field appears in the engine output.

Fall-back behavior (provider raises ``LLMProviderError``) is also exercised:
the engine must produce the deterministic-only result plus a stderr warning.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

import pytest

from fakoli_state.planning.inference import (
    SubtaskProposal,
    expand_task,
)
from fakoli_state.planning.llm import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    RecordedLLMProvider,
)
from fakoli_state.planning.scoring import score_task
from fakoli_state.planning.template import parse_prd
from fakoli_state.state.models import (
    Score,
    Task,
    TaskPriority,
    TaskStatus,
    Verification,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_UTC = datetime.UTC
_NOW = datetime.datetime(2026, 5, 24, 18, 0, 0, tzinfo=_UTC)


def _make_response(text: str) -> LLMResponse:
    """A canned LLMResponse with the given text."""
    return LLMResponse(
        text=text,
        input_tokens=42,
        cached_input_tokens=0,
        output_tokens=20,
        model="claude-sonnet-4-6",
        finish_reason="end_turn",
    )


def _make_task(
    *,
    task_id: str = "T001",
    description: str = "A reasonably long description for the task body.",
    likely_files: list[str] | None = None,
    dependencies: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    title: str = "Test Task",
    complexity: int | None = None,
) -> Task:
    scores = Score()
    if complexity is not None:
        scores = Score(
            complexity=complexity,
            parallelizability=3,
            context_load=3,
            blast_radius=3,
            review_risk=3,
            agent_suitability=6 - complexity if complexity < 5 else 1,
        )
    return Task(
        id=task_id,
        feature_id="F001",
        title=title,
        description=description,
        status=TaskStatus.proposed,
        priority=TaskPriority.medium,
        scores=scores,
        acceptance_criteria=acceptance_criteria or [],
        verification=Verification(commands=["pytest -q"]),
        likely_files=likely_files or [],
        dependencies=dependencies or [],
        conflict_groups=[],
        created_at=_NOW,
        updated_at=_NOW,
    )


class _AlwaysFailingProvider:
    """A provider that always raises LLMProviderError — for fall-back tests."""

    def generate(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        _ = system, user, max_tokens, temperature
        raise LLMProviderError("simulated provider failure for fall-back test")


# ---------------------------------------------------------------------------
# score_task — LLM-augmented explanation
# ---------------------------------------------------------------------------


class TestScoreTaskWithProvider:
    def test_default_no_provider_unchanged(self) -> None:
        """No provider → identical to the pre-Wave-2 deterministic output."""
        task = _make_task(likely_files=["src/foo.py"])
        score = score_task(task)
        assert score.complexity is not None
        # Deterministic explanation contains the per-dimension breakdowns and
        # crucially does NOT contain a Wave-2 augmentation paragraph.
        assert score.explanation is not None
        assert "complexity:" in score.explanation
        # No double-blank-line separator means no augmentation paragraph was appended.
        assert "\n\n" not in score.explanation

    def test_provider_appends_paragraph_to_explanation(self) -> None:
        """With provider, the canned LLM paragraph is appended after a blank line."""
        task = _make_task(likely_files=["src/foo.py"])

        # Build the system prompt the way scoring.py builds it so we can
        # pre-compute the recording key.
        from fakoli_state.planning.scoring import _SCORE_EXPLAIN_SYSTEM_PROMPT

        # First compute the deterministic score so we know what user payload
        # the engine will build (scores must match).
        det_score = score_task(task)

        user_payload = json.dumps(
            {
                "task_id": task.id,
                "title": task.title,
                "description": task.description,
                "likely_files": task.likely_files,
                "dependencies": task.dependencies,
                "scores": {
                    "complexity": det_score.complexity,
                    "parallelizability": det_score.parallelizability,
                    "context_load": det_score.context_load,
                    "blast_radius": det_score.blast_radius,
                    "review_risk": det_score.review_risk,
                    "agent_suitability": det_score.agent_suitability,
                },
            },
            sort_keys=True,
        )
        # Phase 9 C2: record_key includes tuning args, so the test must pass
        # the exact max_tokens the score engine uses (_SCORE_EXPLAIN_MAX_TOKENS).
        from fakoli_state.planning.scoring import _SCORE_EXPLAIN_MAX_TOKENS
        key = RecordedLLMProvider.record_key(
            _SCORE_EXPLAIN_SYSTEM_PROMPT,
            user_payload,
            max_tokens=_SCORE_EXPLAIN_MAX_TOKENS,
        )

        canned = _make_response(
            "This task is moderately complex; the public-API exposure pushes "
            "review risk up but the small file surface keeps blast radius low."
        )
        provider: LLMProvider = RecordedLLMProvider({key: canned})

        score = score_task(task, provider=provider)

        # Numeric scores unchanged.
        assert score.complexity == det_score.complexity
        assert score.parallelizability == det_score.parallelizability

        # Augmented explanation includes both the rule-based breakdown and
        # the LLM paragraph, separated by a blank line.
        assert score.explanation is not None
        assert "complexity:" in score.explanation  # deterministic part
        assert canned.text in score.explanation     # augmented part
        # Augmented paragraph comes AFTER the rule-based breakdown.
        assert score.explanation.index("complexity:") < score.explanation.index(
            canned.text
        )

    def test_provider_failure_falls_back_to_deterministic(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """LLMProviderError → deterministic result + warning on stderr."""
        task = _make_task(likely_files=["src/foo.py"])
        provider = _AlwaysFailingProvider()

        score = score_task(task, provider=provider)
        det_score = score_task(task)  # baseline

        # Numeric scores identical.
        assert score.complexity == det_score.complexity
        # Explanation is the deterministic one (no augmentation appended).
        assert score.explanation == det_score.explanation

        captured = capsys.readouterr()
        assert "warning" in captured.err.lower()
        assert "T001" in captured.err

    def test_empty_llm_text_falls_back(self) -> None:
        """An empty LLM response leaves the deterministic explanation alone."""
        task = _make_task(likely_files=["src/bar.py"])

        from fakoli_state.planning.scoring import _SCORE_EXPLAIN_SYSTEM_PROMPT

        det_score = score_task(task)
        user_payload = json.dumps(
            {
                "task_id": task.id,
                "title": task.title,
                "description": task.description,
                "likely_files": task.likely_files,
                "dependencies": task.dependencies,
                "scores": {
                    "complexity": det_score.complexity,
                    "parallelizability": det_score.parallelizability,
                    "context_load": det_score.context_load,
                    "blast_radius": det_score.blast_radius,
                    "review_risk": det_score.review_risk,
                    "agent_suitability": det_score.agent_suitability,
                },
            },
            sort_keys=True,
        )
        # Phase 9 C2: record_key includes tuning args; pass the engine's
        # _SCORE_EXPLAIN_MAX_TOKENS so the recorded key matches the lookup.
        from fakoli_state.planning.scoring import _SCORE_EXPLAIN_MAX_TOKENS
        key = RecordedLLMProvider.record_key(
            _SCORE_EXPLAIN_SYSTEM_PROMPT,
            user_payload,
            max_tokens=_SCORE_EXPLAIN_MAX_TOKENS,
        )
        # Empty text — the engine should keep the deterministic explanation.
        canned = _make_response("   ")
        provider = RecordedLLMProvider({key: canned})

        score = score_task(task, provider=provider)
        assert score.explanation == det_score.explanation


# ---------------------------------------------------------------------------
# parse_prd — LLM-augmented short Task descriptions
# ---------------------------------------------------------------------------


_PRD_WITH_SHORT_TASK = """\
# Project: Short-Desc Test

## Summary

A test project to exercise LLM description enrichment.

## Goals

- Validate description augmentation.

## Requirements

- R001: Tasks have descriptions.

## Features

### F001: Core

The only feature.

**Requirements:** R001

## Tasks

### T001: Short

**Feature:** F001
**Priority:** medium

Short body.
"""


_PRD_WITH_LONG_TASK = """\
# Project: Long-Desc Test

## Summary

A test project.

## Goals

- Goal.

## Requirements

- R001: Requirement.

## Features

### F001: Core

Feature.

**Requirements:** R001

## Tasks

### T001: Long Task

**Feature:** F001
**Priority:** medium

This is a deliberately long task description that exceeds fifty characters easily and therefore should not trigger LLM enrichment because the deterministic parse already captured enough context.
"""


class TestParsePrdWithProvider:
    def test_default_no_provider_unchanged(self) -> None:
        """parse_prd without provider produces unmodified deterministic output."""
        result = parse_prd(_PRD_WITH_SHORT_TASK)
        assert len(result.tasks) == 1
        # Deterministic short description is preserved exactly.
        assert result.tasks[0].description == "Short body."

    def test_provider_enriches_short_description(self) -> None:
        """A short Task description (<50 chars) is replaced by the LLM output."""
        from fakoli_state.planning.template import _DESCRIPTION_ENRICH_SYSTEM_PROMPT

        # Pre-parse to learn what the deterministic description looks like
        # (so we can build the same user prompt the engine will).
        det = parse_prd(_PRD_WITH_SHORT_TASK)
        det_task = det.tasks[0]
        assert len(det_task.description) < 50  # precondition for enrichment

        user_payload = (
            f"Requirement: {det_task.title}\n"
            f"Existing short description: {det_task.description!r}"
        )
        # Phase 9 C2: record_key includes tuning args; pass the engine's
        # _DESCRIPTION_ENRICH_MAX_TOKENS so the recorded key matches.
        from fakoli_state.planning.template import _DESCRIPTION_ENRICH_MAX_TOKENS
        key = RecordedLLMProvider.record_key(
            _DESCRIPTION_ENRICH_SYSTEM_PROMPT,
            user_payload,
            max_tokens=_DESCRIPTION_ENRICH_MAX_TOKENS,
        )
        canned_text = (
            "Implement the Short module: define the public surface in "
            "src/short.py and cover edge cases in tests/test_short.py. "
            "Honor the existing logging and error-handling patterns."
        )
        provider = RecordedLLMProvider({key: _make_response(canned_text)})

        result = parse_prd(_PRD_WITH_SHORT_TASK, provider=provider)
        assert len(result.tasks) == 1
        assert result.tasks[0].description == canned_text
        # ID and title preserved.
        assert result.tasks[0].id == "T001"
        assert result.tasks[0].title == "Short"

    def test_provider_skips_long_description(self) -> None:
        """A long description (>=50 chars) is NOT sent to the provider.

        Verified by injecting an empty-recordings provider — if the engine
        called it on a long-description task it would raise (key miss) and
        the parse would surface a warning rather than completing cleanly.
        """
        provider = RecordedLLMProvider({})
        result = parse_prd(_PRD_WITH_LONG_TASK, provider=provider)
        assert len(result.tasks) == 1
        # Description unchanged: matches the deterministic parse exactly.
        det = parse_prd(_PRD_WITH_LONG_TASK)
        assert result.tasks[0].description == det.tasks[0].description

    def test_provider_failure_falls_back_per_task(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """LLMProviderError on a short-desc task → deterministic description kept."""
        provider = _AlwaysFailingProvider()
        result = parse_prd(_PRD_WITH_SHORT_TASK, provider=provider)

        assert len(result.tasks) == 1
        # Falls back to the deterministic description.
        assert result.tasks[0].description == "Short body."

        captured = capsys.readouterr()
        assert "warning" in captured.err.lower()
        assert "T001" in captured.err


# ---------------------------------------------------------------------------
# expand_task — LLM-augmented sub-task proposals
# ---------------------------------------------------------------------------


def _expand_user_payload(task: Task) -> str:
    """Reproduce the user payload string built by inference.expand_task."""
    return json.dumps(
        {
            "task_id": task.id,
            "title": task.title,
            "description": task.description,
            "likely_files": task.likely_files,
            "acceptance_criteria": task.acceptance_criteria,
            "scores": {
                "complexity": task.scores.complexity,
                "parallelizability": task.scores.parallelizability,
                "context_load": task.scores.context_load,
                "blast_radius": task.scores.blast_radius,
                "review_risk": task.scores.review_risk,
                "agent_suitability": task.scores.agent_suitability,
            },
        },
        sort_keys=True,
    )


class TestExpandTaskDefaults:
    def test_no_provider_returns_empty(self) -> None:
        """Deterministic engine never invents sub-tasks."""
        task = _make_task(complexity=5)
        assert expand_task(task) == []

    def test_low_complexity_returns_empty_even_with_provider(self) -> None:
        """complexity < 4 → no expansion regardless of provider presence."""
        task = _make_task(complexity=3)
        # Provider with no recordings; if expand_task called it, it would
        # raise LLMProviderError. Test that it doesn't.
        provider = RecordedLLMProvider({})
        assert expand_task(task, provider=provider) == []

    def test_no_complexity_score_returns_empty(self) -> None:
        """A task with no score yet cannot be expanded."""
        task = _make_task()  # default scores=Score() → complexity is None
        provider = RecordedLLMProvider({})
        assert expand_task(task, provider=provider) == []


class TestExpandTaskWithProvider:
    def test_high_complexity_calls_provider_and_returns_proposals(self) -> None:
        """complexity >= 4 + provider → parsed SubtaskProposal list."""
        from fakoli_state.planning.inference import _EXPAND_SYSTEM_PROMPT

        task = _make_task(
            task_id="T042",
            description="A complex multi-module refactor",
            complexity=5,
            likely_files=["src/a.py", "src/b.py"],
            acceptance_criteria=["All tests pass."],
        )

        canned_payload: list[dict[str, Any]] = [
            {
                "title": "Extract module A interface",
                "description": "Pull the public surface of a.py into a Protocol.",
                "acceptance_criteria": ["Protocol declared", "a.py implements it"],
                "likely_files": ["src/a.py", "src/a_protocol.py"],
            },
            {
                "title": "Refactor module B to use A protocol",
                "description": "Adapt b.py to consume the new Protocol.",
                "acceptance_criteria": ["b.py imports the protocol"],
                "likely_files": ["src/b.py"],
            },
        ]
        canned = _make_response(json.dumps(canned_payload))

        user_payload = _expand_user_payload(task)
        # Phase 9 C2: record_key includes tuning args; expand_task uses
        # _EXPAND_MAX_TOKENS, so the test must pass the same value to match.
        from fakoli_state.planning.inference import _EXPAND_MAX_TOKENS
        key = RecordedLLMProvider.record_key(
            _EXPAND_SYSTEM_PROMPT, user_payload, max_tokens=_EXPAND_MAX_TOKENS
        )
        provider = RecordedLLMProvider({key: canned})

        proposals = expand_task(task, provider=provider)
        assert len(proposals) == 2
        assert isinstance(proposals[0], SubtaskProposal)
        assert proposals[0].title == "Extract module A interface"
        assert proposals[0].acceptance_criteria == [
            "Protocol declared",
            "a.py implements it",
        ]
        assert proposals[0].likely_files == ["src/a.py", "src/a_protocol.py"]
        assert proposals[1].title == "Refactor module B to use A protocol"

    def test_provider_failure_returns_empty(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """LLMProviderError → empty proposals + stderr warning, no raise."""
        task = _make_task(complexity=4)
        provider = _AlwaysFailingProvider()
        assert expand_task(task, provider=provider) == []

        captured = capsys.readouterr()
        assert "warning" in captured.err.lower()
        assert "T001" in captured.err

    def test_non_json_response_returns_empty(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Garbled (non-JSON) LLM output → empty + stderr warning."""
        from fakoli_state.planning.inference import (
            _EXPAND_MAX_TOKENS,
            _EXPAND_SYSTEM_PROMPT,
        )

        task = _make_task(complexity=4)
        canned = _make_response("Sorry, I cannot help with that today.")
        # Phase 9 C2: record_key includes tuning args.
        key = RecordedLLMProvider.record_key(
            _EXPAND_SYSTEM_PROMPT,
            _expand_user_payload(task),
            max_tokens=_EXPAND_MAX_TOKENS,
        )
        provider = RecordedLLMProvider({key: canned})

        assert expand_task(task, provider=provider) == []
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower()
        assert "non-JSON" in captured.err

    def test_non_list_json_returns_empty(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """LLM returns a JSON object (not list) → empty + warning."""
        from fakoli_state.planning.inference import (
            _EXPAND_MAX_TOKENS,
            _EXPAND_SYSTEM_PROMPT,
        )

        task = _make_task(complexity=4)
        canned = _make_response('{"wrong": "shape"}')
        # Phase 9 C2: record_key includes tuning args.
        key = RecordedLLMProvider.record_key(
            _EXPAND_SYSTEM_PROMPT,
            _expand_user_payload(task),
            max_tokens=_EXPAND_MAX_TOKENS,
        )
        provider = RecordedLLMProvider({key: canned})

        assert expand_task(task, provider=provider) == []
        captured = capsys.readouterr()
        assert "non-list" in captured.err.lower()

    def test_skips_malformed_items_but_keeps_good_ones(self) -> None:
        """Items missing title are skipped; well-formed siblings still returned."""
        from fakoli_state.planning.inference import (
            _EXPAND_MAX_TOKENS,
            _EXPAND_SYSTEM_PROMPT,
        )

        task = _make_task(complexity=4)
        canned_payload = [
            {"description": "No title here"},
            {
                "title": "Good sub-task",
                "description": "Has everything.",
                "acceptance_criteria": ["AC1"],
                "likely_files": ["src/x.py"],
            },
            {
                "title": "Another good one",
                "description": "Also fine.",
                "acceptance_criteria": [],
                "likely_files": [],
            },
        ]
        canned = _make_response(json.dumps(canned_payload))
        # Phase 9 C2: record_key includes tuning args.
        key = RecordedLLMProvider.record_key(
            _EXPAND_SYSTEM_PROMPT,
            _expand_user_payload(task),
            max_tokens=_EXPAND_MAX_TOKENS,
        )
        provider = RecordedLLMProvider({key: canned})

        proposals = expand_task(task, provider=provider)
        assert len(proposals) == 2
        assert proposals[0].title == "Good sub-task"
        assert proposals[1].title == "Another good one"

    def test_caps_proposals_at_five(self) -> None:
        """If the LLM returns >5 proposals, the engine truncates to 5."""
        from fakoli_state.planning.inference import (
            _EXPAND_MAX_TOKENS,
            _EXPAND_SYSTEM_PROMPT,
        )

        task = _make_task(complexity=4)
        canned_payload = [
            {
                "title": f"Sub-task {i}",
                "description": f"Description {i}",
                "acceptance_criteria": [],
                "likely_files": [],
            }
            for i in range(8)
        ]
        canned = _make_response(json.dumps(canned_payload))
        # Phase 9 C2: record_key includes tuning args.
        key = RecordedLLMProvider.record_key(
            _EXPAND_SYSTEM_PROMPT,
            _expand_user_payload(task),
            max_tokens=_EXPAND_MAX_TOKENS,
        )
        provider = RecordedLLMProvider({key: canned})

        proposals = expand_task(task, provider=provider)
        assert len(proposals) == 5


# ---------------------------------------------------------------------------
# expand_task — LLM-output quirk tolerance (v1.15.0)
# ---------------------------------------------------------------------------


class TestExpandTaskHandlesLlmQuirks:
    """v1.15.0: ``_parse_subtask_response`` now strips markdown code fences
    and falls back to regex-extracting the first JSON array when the LLM
    wraps the response in prose. Before this, every fenced response was
    treated as non-JSON and silently dropped — the user reported that
    `fakoli-state expand --use-llm` returned "non-JSON" for every task."""

    def _setup(self, response_text: str):  # type: ignore[no-untyped-def]
        """Build the recorded-provider machinery for an expand_task call."""
        from fakoli_state.planning.inference import (
            _EXPAND_MAX_TOKENS,
            _EXPAND_SYSTEM_PROMPT,
        )

        task = _make_task(complexity=5, likely_files=["src/foo.py"])
        canned = _make_response(response_text)
        key = RecordedLLMProvider.record_key(
            _EXPAND_SYSTEM_PROMPT,
            _expand_user_payload(task),
            max_tokens=_EXPAND_MAX_TOKENS,
        )
        provider = RecordedLLMProvider({key: canned})
        return task, provider

    _CANNED_PROPOSALS = [
        {
            "title": "First subtask",
            "description": "Do A.",
            "acceptance_criteria": ["A is done"],
            "likely_files": ["src/a.py"],
        },
        {
            "title": "Second subtask",
            "description": "Do B.",
            "acceptance_criteria": ["B is done"],
            "likely_files": ["src/b.py"],
        },
    ]

    def test_fenced_json_response_parses(self) -> None:
        """LLM wraps the JSON array in ```json ... ``` fences — the parser
        strips the fences and parses the inner array. THIS IS THE BUG THE
        USER REPORTED: previously every fenced response was treated as
        non-JSON."""
        fenced = (
            "```json\n" + json.dumps(self._CANNED_PROPOSALS) + "\n```"
        )
        task, provider = self._setup(fenced)
        proposals = expand_task(task, provider=provider)
        assert len(proposals) == 2
        assert proposals[0].title == "First subtask"

    def test_unlabeled_fence_response_parses(self) -> None:
        """Same as fenced JSON but with bare ``` (no `json` language tag)."""
        fenced = "```\n" + json.dumps(self._CANNED_PROPOSALS) + "\n```"
        task, provider = self._setup(fenced)
        proposals = expand_task(task, provider=provider)
        assert len(proposals) == 2

    def test_json_with_leading_prose_extracted(self) -> None:
        """LLM prepends 'Here are 2 sub-tasks:' before the array. The
        regex-extract-first-array fallback rescues this."""
        wrapped = (
            "Here are 2 sub-tasks for this complex refactor:\n\n"
            + json.dumps(self._CANNED_PROPOSALS)
        )
        task, provider = self._setup(wrapped)
        proposals = expand_task(task, provider=provider)
        assert len(proposals) == 2
        assert proposals[0].title == "First subtask"

    def test_empty_response_warns_specifically(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """Empty LLM response gets a specific empty-response warning, not
        the generic non-JSON one."""
        task, provider = self._setup("")
        proposals = expand_task(task, provider=provider)
        assert proposals == []
        _, err = capfd.readouterr()
        assert "empty response" in err.lower()

    def test_truly_non_json_response_includes_sample_in_warning(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """When the response really cannot be parsed (no fences, no
        bracketed array, just prose), the warning includes a sample of
        what the LLM wrote so the user can debug without re-running."""
        garbage = (
            "I cannot decompose this task because the requirements are "
            "ambiguous. Please clarify the acceptance criteria first."
        )
        task, provider = self._setup(garbage)
        proposals = expand_task(task, provider=provider)
        assert proposals == []
        _, err = capfd.readouterr()
        assert "I cannot decompose" in err, (
            f"warning should quote the LLM response so user can debug; "
            f"stderr was: {err}"
        )
