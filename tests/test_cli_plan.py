"""CLI tests for ``fakoli-state expand`` — Phase 9 T6 (C4: --format prd).

Greenfield test module created in Phase 9 to cover the new ``--format``
option on the ``expand`` subcommand (added by Phase 9 T6 / C4) and to lock
in the legacy ``--format text`` behavior as a baseline before later changes.

Coverage:
- ``--format text`` (default) — legacy per-subtask block output unchanged.
- ``--format prd`` — emits markdown blocks matching ``docs/prd-template.md``.
- ``--format <invalid>`` — exits 1 with a clean error message.
- Validation precedence — ``--format`` is checked BEFORE the ``--use-llm``
  guard so a user passing both bad flags sees the clearer error first.

Pattern: monkeypatch ``fakoli_state.cli.plan._resolve_llm_provider`` to
return an in-test fake provider that returns canned proposals.  This is
the same pattern used by ``tests/test_cli.py::TestUseLlmRecordedProvider``
(line ~1815) — kept consistent so a future helper extraction can share the
same monkeypatch shape.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fakoli_state.cli import app
from fakoli_state.planning.llm import LLMResponse

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers (mirrored from tests/test_cli.py to keep this module self-contained;
# extracting to a conftest fixture is a Phase 10+ tidy-up, not required here.)
# ---------------------------------------------------------------------------


def _do_init(tmp_path: Path, name: str = "Expand Test Project") -> None:
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(
            app, ["init", "--name", name], catch_exceptions=False
        )
        assert result.exit_code == 0, f"init failed: {result.output}"
    finally:
        os.chdir(original_cwd)


def _write_prd(tmp_path: Path, content: str) -> None:
    prd_path = tmp_path / ".fakoli-state" / "prd.md"
    prd_path.write_text(content, encoding="utf-8")


def _invoke_cmd(tmp_path: Path, cmd: list[str]):  # type: ignore[no-untyped-def]
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(app, cmd, catch_exceptions=False)
    finally:
        os.chdir(original_cwd)
    return result


def _install_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider_factory: Callable[[], Any],
) -> None:
    """Replace ``_resolve_llm_provider`` so the test never touches network/env."""
    import importlib

    plan_module = importlib.import_module("fakoli_state.cli.plan")

    def fake_resolve(use_llm: bool, config=None):  # type: ignore[no-untyped-def]
        return provider_factory() if use_llm else None

    monkeypatch.setattr(plan_module, "_resolve_llm_provider", fake_resolve)


# A PRD that produces a single high-complexity (>=4) task so expand has
# something to decompose.  Complexity heuristics treat >=5 likely files as
# "high complexity" — see ``planning/scoring.py``.
_COMPLEX_TASK_PRD = """\
# Project: Expand Format Test

## Summary

Project for the expand --format CLI tests.

## Goals

- Decompose complex tasks.

## Requirements

- R001: Big refactor.

## Features

### F001: Big Refactor

The only feature.

**Requirements:** R001

## Tasks

### T001: Decompose-this large planning-engine refactor

**Feature:** F001
**Priority:** high
**Likely files:** src/a.py, src/b.py, src/c.py, src/d.py, src/e.py, src/f.py

**Acceptance criteria:**

- Refactor compiles.
- Migration story documented.

**Verification:**

- `pytest -q`

A refactor that touches multiple modules and warrants sub-task expansion.
"""


def _canned_proposals_text() -> str:
    """JSON payload the fake provider returns when ``generate()`` is called."""
    return json.dumps(
        [
            {
                "title": "Extract module A interface",
                "description": (
                    "Pull the public surface of a.py into a typed Protocol so "
                    "b.py and c.py can depend on the abstraction, not the "
                    "concretion."
                ),
                "acceptance_criteria": [
                    "Protocol declared in src/a_protocol.py.",
                    "a.py implements the Protocol.",
                ],
                "likely_files": ["src/a.py", "src/a_protocol.py"],
            },
            {
                "title": "Refactor module B to use A protocol",
                "description": "Adapt b.py to consume the new Protocol.",
                "acceptance_criteria": ["b.py imports the protocol."],
                "likely_files": ["src/b.py"],
            },
        ]
    )


class _AlwaysReturnProvider:
    """Fake LLM provider that returns the canned proposals payload.

    Bypasses ``RecordedLLMProvider``'s key-matching so the test does not
    have to re-derive the engine's user-payload JSON.  Equivalent to the
    ``_AlwaysReturnProvider`` defined inline in tests/test_cli.py.
    """

    def __init__(self, text: str | None = None) -> None:
        self._text = text if text is not None else _canned_proposals_text()

    def generate(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        _ = system, user, max_tokens, temperature
        return LLMResponse(
            text=self._text,
            input_tokens=10,
            cached_input_tokens=0,
            output_tokens=80,
            model="claude-sonnet-4-6",
            finish_reason="end_turn",
        )


def _bootstrap_expanded_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_factory: Callable[[], Any] | None = None,
) -> None:
    """Set up a project with a single high-complexity T001 ready to expand."""
    _install_provider(
        monkeypatch, provider_factory or (lambda: _AlwaysReturnProvider())
    )
    _do_init(tmp_path)
    _write_prd(tmp_path, _COMPLEX_TASK_PRD)
    _invoke_cmd(tmp_path, ["prd", "parse"])
    _invoke_cmd(tmp_path, ["plan"])
    _invoke_cmd(tmp_path, ["score"])


# ---------------------------------------------------------------------------
# --format text — baseline (legacy behavior unchanged)
# ---------------------------------------------------------------------------


class TestExpandFormatText:
    """Baseline: --format text matches the pre-Phase-9 human-readable output."""

    def test_default_format_is_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Omitting --format defaults to text — the legacy per-subtask block."""
        _bootstrap_expanded_task(tmp_path, monkeypatch)

        result = _invoke_cmd(tmp_path, ["expand", "T001", "--use-llm"])
        assert result.exit_code == 0, f"expand failed: {result.output}"

        # Legacy summary line.
        assert "Proposed 2 sub-task" in result.output
        assert "Paste into prd.md as ### TXxx" in result.output
        # Per-subtask blocks use the legacy --- delimiter.
        assert "--- Sub-task 1 ---" in result.output
        assert "--- Sub-task 2 ---" in result.output
        # Field labels are the legacy "Title:" / "Description:" prose form,
        # NOT the PRD `### T001.N:` heading form.
        assert "Title: Extract module A interface" in result.output
        assert "Title: Refactor module B to use A protocol" in result.output
        # Legacy mode does NOT emit the PRD H3 heading shape.
        assert "### T001.1" not in result.output
        assert "### T001.2" not in result.output

    def test_explicit_format_text_matches_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--format text`` is identical to omitting --format."""
        _bootstrap_expanded_task(tmp_path, monkeypatch)

        default = _invoke_cmd(tmp_path, ["expand", "T001", "--use-llm"])
        explicit = _invoke_cmd(
            tmp_path, ["expand", "T001", "--use-llm", "--format", "text"]
        )
        assert default.exit_code == 0
        assert explicit.exit_code == 0
        assert default.output == explicit.output


# ---------------------------------------------------------------------------
# --format prd — Phase 9 C4 — markdown blocks matching docs/prd-template.md
# ---------------------------------------------------------------------------


class TestExpandFormatPrd:
    """Phase 9 C4: --format prd emits paste-ready markdown blocks."""

    def test_prd_format_emits_subtask_headings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each proposal becomes a ### T001.N: <title> H3 block."""
        _bootstrap_expanded_task(tmp_path, monkeypatch)

        result = _invoke_cmd(
            tmp_path, ["expand", "T001", "--use-llm", "--format", "prd"]
        )
        assert result.exit_code == 0, f"expand --format prd failed: {result.output}"

        # PRD heading shape per docs/prd-template.md "ID Conventions": T001.1, T001.2.
        assert "### T001.1: Extract module A interface" in result.output
        assert "### T001.2: Refactor module B to use A protocol" in result.output

    def test_prd_format_includes_template_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each block includes the **Feature:**, **Priority:**, **Likely files:**,
        **Acceptance criteria:**, **Verification:** fields the PRD parser
        recognises (docs/prd-template.md ## Tasks section).

        Phase 9 critic CONSIDER fix: ``**Feature:**`` is populated from the
        parent task's ``feature_id`` (``F001`` from the test PRD), and
        ``**Priority:**`` from the parent's priority (``high`` from the test
        PRD).  Defaults (blank Feature, ``medium`` Priority) only fire when
        the helper is called without the parent context — a path the CLI
        no longer takes.
        """
        _bootstrap_expanded_task(tmp_path, monkeypatch)

        result = _invoke_cmd(
            tmp_path, ["expand", "T001", "--use-llm", "--format", "prd"]
        )
        assert result.exit_code == 0
        out = result.output

        # All four PRD-template field labels must appear, populated from
        # the parent task's metadata (Phase 9 CONSIDER fix).
        assert "**Feature:** F001" in out
        assert "**Priority:** high" in out  # inherited from parent T001
        assert "**Likely files:** src/a.py, src/a_protocol.py" in out
        assert "**Likely files:** src/b.py" in out
        assert "**Acceptance criteria:**" in out
        assert "**Verification:**" in out

    def test_prd_format_preserves_acceptance_criteria_bullets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Acceptance criteria are emitted as bulleted lines, parser-compatible."""
        _bootstrap_expanded_task(tmp_path, monkeypatch)

        result = _invoke_cmd(
            tmp_path, ["expand", "T001", "--use-llm", "--format", "prd"]
        )
        assert result.exit_code == 0
        out = result.output

        # Bullets must use the `- ` PRD convention.
        assert "- Protocol declared in src/a_protocol.py." in out
        assert "- a.py implements the Protocol." in out
        assert "- b.py imports the protocol." in out

    def test_prd_format_emits_description_paragraph(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The free-form description paragraph from the proposal appears verbatim."""
        _bootstrap_expanded_task(tmp_path, monkeypatch)

        result = _invoke_cmd(
            tmp_path, ["expand", "T001", "--use-llm", "--format", "prd"]
        )
        assert result.exit_code == 0
        assert (
            "Pull the public surface of a.py into a typed Protocol"
            in result.output
        )
        assert "Adapt b.py to consume the new Protocol." in result.output

    def test_prd_format_suppresses_legacy_block_delimiter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PRD mode replaces the ``--- Sub-task N ---`` markers with H3 headings."""
        _bootstrap_expanded_task(tmp_path, monkeypatch)

        result = _invoke_cmd(
            tmp_path, ["expand", "T001", "--use-llm", "--format", "prd"]
        )
        assert result.exit_code == 0
        # The legacy delimiter MUST NOT appear in PRD mode — otherwise the
        # paste-and-go promise is broken (prd.md parsing would choke on it).
        assert "--- Sub-task" not in result.output
        # And the legacy "Paste into prd.md as ### TXxx blocks" hint is
        # replaced by the more specific PRD-mode hint.
        assert "Paste into prd.md as ### TXxx" not in result.output

    def test_prd_format_output_round_trips_to_prd_parser(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: the prd-mode output, pasted into prd.md, parses cleanly.

        This is the load-bearing acceptance criterion for C4 — if a user
        copies the emitted blocks into the ## Tasks section, ``prd parse``
        must accept them without raising.  Verifies the renderer hews to the
        exact shape ``planning/template.parse_prd`` recognises.
        """
        _bootstrap_expanded_task(tmp_path, monkeypatch)

        result = _invoke_cmd(
            tmp_path, ["expand", "T001", "--use-llm", "--format", "prd"]
        )
        assert result.exit_code == 0

        # Strip the leading hint comment line (starts with "# N sub-task block(s)")
        # so what remains is pure PRD ## Tasks content.
        prd_blocks = "\n".join(
            line for line in result.output.splitlines()
            if not line.startswith("# ")
            # Drop the hint comment but keep ### task headings (they start with ###).
        ).strip()
        # The blocks must include both subtask IDs and field labels.
        assert "### T001.1:" in prd_blocks
        assert "### T001.2:" in prd_blocks

        # Compose a minimal valid PRD wrapping the emitted blocks under ## Tasks.
        wrapped_prd = (
            "# Project: Round-Trip Test\n\n"
            "## Summary\n\nRound-trip the expand --format prd output.\n\n"
            "## Goals\n\n- Validate emit.\n\n"
            "## Requirements\n\n- R001: Round-trip.\n\n"
            "## Features\n\n### F001: Core\n\nFeature.\n\n**Requirements:** R001\n\n"
            "## Tasks\n\n"
            "### T001: Parent task\n\n"
            "**Feature:** F001\n"
            "**Priority:** medium\n\n"
            "Parent body.\n\n"
            "**Acceptance criteria:**\n\n- AC.\n\n"
            "**Verification:**\n\n- `pytest -q`\n\n"
            f"{prd_blocks}\n"
        )

        from fakoli_state.planning.template import parse_prd

        parsed = parse_prd(wrapped_prd, prd_id="prd-roundtrip")
        # No fatal errors from the parser.
        fatal = [e for e in parsed.errors if "fatal" in e.message.lower()]
        assert not fatal, f"parse_prd raised fatal errors: {fatal}"
        # The parent and both subtasks all appear as Tasks.
        task_ids = {t.id for t in parsed.tasks}
        assert "T001" in task_ids
        assert "T001.1" in task_ids, (
            f"T001.1 not parsed from emitted block; got {sorted(task_ids)}; "
            f"errors={parsed.errors}"
        )
        assert "T001.2" in task_ids, (
            f"T001.2 not parsed from emitted block; got {sorted(task_ids)}; "
            f"errors={parsed.errors}"
        )


# ---------------------------------------------------------------------------
# --format validation
# ---------------------------------------------------------------------------


class TestExpandFormatValidation:
    """--format rejects values outside the {text, prd} set."""

    def test_invalid_format_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--format json`` exits 1 with a message naming the invalid value."""
        _bootstrap_expanded_task(tmp_path, monkeypatch)

        result = _invoke_cmd(
            tmp_path, ["expand", "T001", "--use-llm", "--format", "json"]
        )
        assert result.exit_code == 1
        # The error message names the invalid value and lists the accepted set.
        assert "--format" in result.output or "format" in result.output.lower()
        assert "text" in result.output
        assert "prd" in result.output

    def test_format_validation_runs_before_use_llm_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--format <bad>`` without --use-llm still surfaces the format error.

        The format check runs first so the user fixes the typo before the CLI
        starts complaining about flags they have not yet noticed are missing.
        """
        _do_init(tmp_path)
        _write_prd(tmp_path, _COMPLEX_TASK_PRD)
        _invoke_cmd(tmp_path, ["prd", "parse"])

        result = _invoke_cmd(tmp_path, ["expand", "T001", "--format", "xml"])
        assert result.exit_code == 1
        # The error must be about --format, not about --use-llm.
        assert "format" in result.output.lower()
        # The message should NOT lead with the --use-llm error.
        assert not re.match(r"\s*Error:\s*expand requires --use-llm", result.output)


# ---------------------------------------------------------------------------
# --format help text
# ---------------------------------------------------------------------------


class TestExpandFormatHelp:
    def test_expand_help_documents_format_option(self) -> None:
        """The --format option appears in `expand --help` output."""
        result = runner.invoke(app, ["expand", "--help"])
        assert result.exit_code == 0
        assert "--format" in result.output
        # Reference both supported values so users discover them from --help.
        assert "text" in result.output
        assert "prd" in result.output
