"""CLI tests for ``anvil expand`` — Phase 9 T6 (C4: --format prd).

Greenfield test module created in Phase 9 to cover the new ``--format``
option on the ``expand`` subcommand (added by Phase 9 T6 / C4) and to lock
in the legacy ``--format text`` behavior as a baseline before later changes.

Coverage:
- ``--format text`` (default) — legacy per-subtask block output unchanged.
- ``--format prd`` — emits markdown blocks matching ``docs/prd-template.md``.
- ``--format <invalid>`` — exits 1 with a clean error message.
- Validation precedence — ``--format`` is checked BEFORE the ``--use-llm``
  guard so a user passing both bad flags sees the clearer error first.

Pattern: monkeypatch ``anvil.cli.plan._resolve_llm_provider`` to
return an in-test fake provider that returns canned proposals.  This is
the same pattern used by ``tests/test_cli.py::TestUseLlmRecordedProvider``
(line ~1815) — kept consistent so a future helper extraction can share the
same monkeypatch shape.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.clock import FrozenClock
from anvil.planning._plan_helpers import (
    DEPENDENCY_CYCLE_MESSAGE,
    DEPENDENCY_EDGE_FORMAT_MESSAGE,
    DEPENDENCY_SELF_LOOP_MESSAGE,
    DEPENDENCY_UNKNOWN_SOURCE_MESSAGE,
    BatchDepError,
    BatchDepPlan,
    DepEdge,
    emit_batch_dep_events,
    parse_dep_edge,
    plan_batch_dep_edits,
)
from anvil.planning.llm import LLMResponse
from anvil.state.backend import EventRejected
from anvil.state.models import Task

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
    prd_path = tmp_path / ".anvil" / "prd.md"
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

    plan_module = importlib.import_module("anvil.cli.plan")

    def fake_resolve(use_llm: bool, config=None, model=None):  # type: ignore[no-untyped-def]
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

        from anvil.planning.template import parse_prd

        # Use the default prd_id so ids stay BARE (T001, T001.1) — prd_id is
        # now load-bearing and a named PRD would prefix them (T015).
        parsed = parse_prd(wrapped_prd)
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


# ---------------------------------------------------------------------------
# batch_deps — `anvil deps` batch dependency-edit primitive (T022/F007)
# ---------------------------------------------------------------------------


def _seed_dep_tasks(tmp_path: Path, ids_with_deps: list[tuple[str, list[str]]]) -> None:
    """Seed a feature + tasks with explicit dependency lists via raw SQLite.

    Mirrors the ``_seed_graph_tasks`` idiom in tests/test_cli.py: inserting
    directly keeps the starting graph fixed regardless of planner behaviour, so
    the batch-deps assertions are deterministic. Every task is created in
    ``ready`` status so we can also assert status is preserved across the
    dependency-only upserts the ``deps`` command emits.
    """
    _do_init(tmp_path, name="Deps Test Project")
    db_path = tmp_path / ".anvil" / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR IGNORE INTO features "
        "(id, title, description, status, requirements, tasks) "
        "VALUES ('F001', 'Deps Feature', 'desc', 'proposed', '[]', '[]')"
    )
    for task_id, deps in ids_with_deps:
        conn.execute(
            """INSERT OR REPLACE INTO tasks
            (id, feature_id, title, description, status, priority, task_type,
             dependencies, conflict_groups, scores, acceptance_criteria,
             implementation_notes, verification, likely_files,
             parent_task_id, created_at, updated_at)
            VALUES (?, 'F001', ?, 'desc', 'ready', 'medium', 'feature',
             ?, '[]', '{}', '["x"]', '[]', '{}', '[]',
             NULL, '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00')""",
            (task_id, f"Title {task_id}", json.dumps(deps)),
        )
    conn.commit()
    conn.close()


def _deps_of(tmp_path: Path, task_id: str) -> list[str]:
    """Read a task's persisted dependency list straight from state.db."""
    db_path = tmp_path / ".anvil" / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT dependencies FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"task {task_id} not found"
    return list(json.loads(row[0]))


def _status_of(tmp_path: Path, task_id: str) -> str:
    db_path = tmp_path / ".anvil" / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


class TestBatchDepsApply:
    """A validated request can apply many dependency edges in one invocation."""

    def test_named_prd_is_explicit_in_dependency_upsert_payload(self) -> None:
        """Dependency upserts preserve named-PRD ownership in the event log."""
        now = datetime(2026, 7, 21, tzinfo=UTC)
        task = Task(
            id="named:T002",
            feature_id="named:F001",
            prd_id="named",
            title="Named task",
            description="desc",
            dependencies=[],
            created_at=now,
            updated_at=now,
        )

        class CaptureBackend:
            def __init__(self) -> None:
                self.drafts: list[Any] = []

            def append(self, draft: Any) -> None:
                self.drafts.append(draft)

        backend = CaptureBackend()
        changed = emit_batch_dep_events(
            backend,  # type: ignore[arg-type]
            {task.id: task},
            BatchDepPlan(
                new_dependencies={task.id: ["named:T001"]},
                added=[(task.id, "named:T001")],
                removed=[],
            ),
            actor="test",
            clock=FrozenClock(now),
        )

        assert changed == [task.id]
        assert backend.drafts[0].payload_json["prd_id"] == "named"
        assert backend.drafts[0].payload_json["dependencies"] == ["named:T001"]

    def test_batch_deps_applies_ten_validated_edges(self, tmp_path: Path) -> None:
        """Ten add-edges across many tasks land in a single `deps` invocation."""
        # 11 independent tasks, no starting deps.
        ids = [f"T0{n:02d}" for n in range(1, 12)]
        _seed_dep_tasks(tmp_path, [(tid, []) for tid in ids])

        # Build a chain T002->T001, T003->T002, ... T011->T010 (10 edges).
        add_args: list[str] = []
        for n in range(2, 12):
            add_args += ["--add", f"T0{n:02d}:T0{n - 1:02d}"]

        result = _invoke_cmd(tmp_path, ["deps", *add_args, "--json"])
        assert result.exit_code == 0, f"deps failed: {result.output}"
        envelope = json.loads(result.output)
        assert envelope["ok"] is True
        assert envelope["command"] == "deps"
        # All 10 edges added; 10 tasks (T002..T011) changed.
        assert len(envelope["data"]["added"]) == 10
        assert len(envelope["data"]["changed"]) == 10

        # Every edge persisted with the correct orientation (source depends on target).
        for n in range(2, 12):
            assert _deps_of(tmp_path, f"T0{n:02d}") == [f"T0{n - 1:02d}"]
        # T001 (the sink) gained nothing and was not touched.
        assert _deps_of(tmp_path, "T001") == []

    def test_batch_deps_preserves_status_on_edited_tasks(self, tmp_path: Path) -> None:
        """Dependency-only edits never regress task status (upsert omits status)."""
        _seed_dep_tasks(tmp_path, [("T001", []), ("T002", [])])
        result = _invoke_cmd(tmp_path, ["deps", "--add", "T002:T001", "--json"])
        assert result.exit_code == 0, result.output
        assert _deps_of(tmp_path, "T002") == ["T001"]
        # Seeded as 'ready'; the dependency edit must leave status untouched.
        assert _status_of(tmp_path, "T002") == "ready"

    @pytest.mark.parametrize("json_output", [True, False])
    def test_batch_deps_translates_backend_rejection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        json_output: bool,
    ) -> None:
        """A backend refusal is stable on both CLI output surfaces."""
        import anvil.planning._plan_helpers as plan_helpers

        marker = "SYNTHETIC_BACKEND_VALIDATION_DETAIL"

        def reject_emit(*args: Any, **kwargs: Any) -> list[str]:
            _ = (args, kwargs)
            raise EventRejected(marker)

        _seed_dep_tasks(tmp_path, [("T001", []), ("T002", [])])
        monkeypatch.setattr(plan_helpers, "emit_batch_dep_events", reject_emit)
        args = ["deps", "--add", "T002:T001"]
        if json_output:
            args.append("--json")

        result = _invoke_cmd(tmp_path, args)

        assert result.exit_code == 1
        assert marker not in result.output
        assert _deps_of(tmp_path, "T002") == []
        if json_output:
            envelope = json.loads(result.output)
            assert envelope == {
                "ok": False,
                "command": "deps",
                "error": {
                    "code": "event_rejected",
                    "message": "dependency update was rejected by state validation.",
                },
            }
            assert len(result.output.encode("utf-8")) <= 4096
        else:
            assert result.output.strip() == (
                "Error: dependency update was rejected by state validation."
            )

    def test_batch_deps_mixed_add_and_remove(self, tmp_path: Path) -> None:
        """A single batch can both add and remove edges."""
        _seed_dep_tasks(
            tmp_path,
            [("T001", []), ("T002", ["T001"]), ("T003", [])],
        )
        result = _invoke_cmd(
            tmp_path,
            [
                "deps",
                "--remove",
                "T002:T001",
                "--add",
                "T003:T001",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["removed"] == [["T002", "T001"]]
        assert data["added"] == [["T003", "T001"]]
        assert _deps_of(tmp_path, "T002") == []
        assert _deps_of(tmp_path, "T003") == ["T001"]


class TestBatchDepsCycleRejected:
    """A batch that introduces a cycle is rejected with NO partial application."""

    def test_batch_deps_rejects_cycle_no_partial_apply(self, tmp_path: Path) -> None:
        """T001->T002 + T002->T001 forms a 2-cycle → whole batch rejected."""
        _seed_dep_tasks(tmp_path, [("T001", []), ("T002", [])])

        result = _invoke_cmd(
            tmp_path,
            [
                "deps",
                "--add",
                "T001:T002",
                "--add",
                "T002:T001",
                "--json",
            ],
        )
        assert result.exit_code == 1, f"expected rejection, got: {result.output}"
        envelope = json.loads(result.output)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "cycle"

        # NO partial application: neither task gained a dependency.
        assert _deps_of(tmp_path, "T001") == []
        assert _deps_of(tmp_path, "T002") == []

    def test_batch_deps_rejects_self_loop(self, tmp_path: Path) -> None:
        """A self-dependency is rejected before any mutation."""
        _seed_dep_tasks(tmp_path, [("T001", [])])
        result = _invoke_cmd(
            tmp_path, ["deps", "--add", "T001:T001", "--json"]
        )
        assert result.exit_code != 0
        envelope = json.loads(result.output)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "self_loop"
        assert _deps_of(tmp_path, "T001") == []

    def test_batch_deps_rejects_unknown_task(self, tmp_path: Path) -> None:
        """An edge referencing a missing task rejects the whole batch."""
        _seed_dep_tasks(tmp_path, [("T001", []), ("T002", [])])
        result = _invoke_cmd(
            tmp_path,
            [
                "deps",
                "--add",
                "T002:T001",  # valid
                "--add",
                "T002:T999",  # T999 does not exist
                "--json",
            ],
        )
        assert result.exit_code != 0
        envelope = json.loads(result.output)
        assert envelope["error"]["code"] == "unknown_task"
        # The valid edge in the same batch must NOT have been applied.
        assert _deps_of(tmp_path, "T002") == []

    def test_batch_deps_rejects_longer_cycle(self, tmp_path: Path) -> None:
        """A 3-task cycle introduced by the batch is detected and rejected."""
        _seed_dep_tasks(
            tmp_path,
            [("T001", []), ("T002", ["T001"]), ("T003", ["T002"])],
        )
        # Adding T001 depends on T003 closes the loop T001->T003->T002->T001.
        result = _invoke_cmd(
            tmp_path, ["deps", "--add", "T001:T003", "--json"]
        )
        assert result.exit_code == 1
        envelope = json.loads(result.output)
        assert envelope["error"]["code"] == "cycle"
        assert _deps_of(tmp_path, "T001") == []

    def test_deep_graph_cycle_detection_is_iterative_and_correct(self) -> None:
        """A 1,500-task chain is valid, while its closing edge is a cycle."""
        now = datetime(2026, 7, 21, tzinfo=UTC)
        ids = [f"T{index:04d}" for index in range(1_500)]
        tasks = [
            Task(
                id=task_id,
                feature_id="F001",
                title=task_id,
                description="deep graph",
                dependencies=([ids[index + 1]] if index + 1 < len(ids) else []),
                created_at=now,
                updated_at=now,
            )
            for index, task_id in enumerate(ids)
        ]

        assert plan_batch_dep_edits(tasks, []) == BatchDepPlan()
        with pytest.raises(BatchDepError) as raised:
            plan_batch_dep_edits(
                tasks,
                [DepEdge(op="add", source=ids[-1], target=ids[0])],
            )

        assert raised.value.code == "cycle"
        assert raised.value.message == DEPENDENCY_CYCLE_MESSAGE


class TestBatchDepsValidation:
    """Edge-spec parsing and empty-batch guards."""

    def test_batch_deps_no_edges_errors(self, tmp_path: Path) -> None:
        """`deps` with neither --add nor --remove is a usage error."""
        _seed_dep_tasks(tmp_path, [("T001", [])])
        result = _invoke_cmd(tmp_path, ["deps", "--json"])
        assert result.exit_code != 0
        assert json.loads(result.output)["error"]["code"] == "bad_request"

    def test_batch_deps_malformed_spec_errors(self, tmp_path: Path) -> None:
        """A spec missing the SOURCE:TARGET separator is rejected."""
        _seed_dep_tasks(tmp_path, [("T001", []), ("T002", [])])
        result = _invoke_cmd(tmp_path, ["deps", "--add", "T002T001", "--json"])
        assert result.exit_code != 0
        assert json.loads(result.output)["error"]["code"] == "bad_request"

    def test_batch_deps_arrow_separator_accepted(self, tmp_path: Path) -> None:
        """The canonical 'SOURCE->TARGET' arrow form is accepted."""
        _seed_dep_tasks(tmp_path, [("T001", []), ("T002", [])])
        result = _invoke_cmd(
            tmp_path, ["deps", "--add", "T002->T001", "--json"]
        )
        assert result.exit_code == 0, result.output
        assert _deps_of(tmp_path, "T002") == ["T001"]

    def test_batch_deps_arrow_preserves_scoped_ids(self) -> None:
        """Colon-bearing PRD scopes remain part of both IDs with an arrow."""
        edge = parse_dep_edge("named:T002->named:T001", "add")

        assert edge.source == "named:T002"
        assert edge.target == "named:T001"

    @pytest.mark.parametrize(
        "raw",
        [
            "T002:named:T001",
            "named:T002:T001",
            "named:T002:named:T001",
            "T002::T001",
        ],
    )
    def test_batch_deps_parser_rejects_ambiguous_scoped_colon_form(
        self, raw: str
    ) -> None:
        """Any extra colon fails closed with fixed arrow guidance."""
        with pytest.raises(BatchDepError) as raised:
            parse_dep_edge(raw, "add")

        assert raised.value.code == "bad_request"
        assert raised.value.message == DEPENDENCY_EDGE_FORMAT_MESSAGE
        assert len(raised.value.message.encode("utf-8")) <= 4096
        assert raw not in raised.value.message

    @pytest.mark.parametrize(
        "raw",
        [
            "T002:named:T001",
            "named:T002:T001",
            "named:T002:named:T001",
        ],
        ids=["default-to-named", "named-to-default", "named-to-named"],
    )
    @pytest.mark.parametrize("json_output", [True, False], ids=["json", "human"])
    def test_batch_deps_cli_rejects_ambiguous_scoped_colon_without_mutation(
        self, tmp_path: Path, raw: str, json_output: bool
    ) -> None:
        """Both CLI surfaces reject before touching events or dependencies."""
        _seed_dep_tasks(tmp_path, [("T001", []), ("T002", [])])
        events_path = tmp_path / ".anvil" / "events.jsonl"
        events_before = events_path.read_bytes()
        args = ["deps", "--add", raw]
        if json_output:
            args.append("--json")

        result = _invoke_cmd(tmp_path, args)

        assert result.exit_code == (1 if json_output else 2)
        assert events_path.read_bytes() == events_before
        assert _deps_of(tmp_path, "T001") == []
        assert _deps_of(tmp_path, "T002") == []
        assert raw not in result.output
        assert len(result.output.encode("utf-8")) <= 4096
        if json_output:
            assert json.loads(result.output) == {
                "ok": False,
                "command": "deps",
                "error": {
                    "code": "bad_request",
                    "message": DEPENDENCY_EDGE_FORMAT_MESSAGE,
                },
            }
        else:
            assert result.output.strip() == (
                f"Error: {DEPENDENCY_EDGE_FORMAT_MESSAGE}"
            )

    @pytest.mark.parametrize(
        "raw",
        [
            "T001->T002->T003",
            "T001->" + ("SYNTHETIC_ARROW_PAYLOAD" * 5_000) + "->T002",
        ],
        ids=["repeated-arrow", "huge-repeated-arrow"],
    )
    def test_batch_deps_parser_rejects_repeated_arrow(self, raw: str) -> None:
        """Arrow syntax is exactly two tokens, even for attacker-sized input."""
        with pytest.raises(BatchDepError) as raised:
            parse_dep_edge(raw, "add")

        assert raised.value.code == "bad_request"
        assert raised.value.message == DEPENDENCY_EDGE_FORMAT_MESSAGE
        assert len(raised.value.message.encode("utf-8")) <= 4096
        assert raw not in raised.value.message
        assert "SYNTHETIC_ARROW_PAYLOAD" not in raised.value.message

    @pytest.mark.parametrize(
        "raw",
        [
            "T001->T002->T003",
            "T001->" + ("SYNTHETIC_ARROW_PAYLOAD" * 5_000) + "->T002",
        ],
        ids=["repeated-arrow", "huge-repeated-arrow"],
    )
    @pytest.mark.parametrize("json_output", [True, False], ids=["json", "human"])
    def test_batch_deps_cli_rejects_repeated_arrow_without_mutation(
        self, tmp_path: Path, raw: str, json_output: bool
    ) -> None:
        """Repeated arrows fail before state opens on both CLI surfaces."""
        _seed_dep_tasks(tmp_path, [("T001", []), ("T002", []), ("T003", [])])
        events_path = tmp_path / ".anvil" / "events.jsonl"
        events_before = events_path.read_bytes()
        args = ["deps", "--add", raw]
        if json_output:
            args.append("--json")

        result = _invoke_cmd(tmp_path, args)

        assert result.exit_code == (1 if json_output else 2)
        assert events_path.read_bytes() == events_before
        for task_id in ("T001", "T002", "T003"):
            assert _deps_of(tmp_path, task_id) == []
        assert raw not in result.output
        assert "SYNTHETIC_ARROW_PAYLOAD" not in result.output
        assert len(result.output.encode("utf-8")) <= 4096
        if json_output:
            assert json.loads(result.output) == {
                "ok": False,
                "command": "deps",
                "error": {
                    "code": "bad_request",
                    "message": DEPENDENCY_EDGE_FORMAT_MESSAGE,
                },
            }
        else:
            assert result.output.strip() == (
                f"Error: {DEPENDENCY_EDGE_FORMAT_MESSAGE}"
            )

    @pytest.mark.parametrize(
        ("case", "expected_code", "expected_message", "human_exit"),
        [
            ("malformed", "bad_request", DEPENDENCY_EDGE_FORMAT_MESSAGE, 2),
            (
                "unknown",
                "unknown_task",
                DEPENDENCY_UNKNOWN_SOURCE_MESSAGE,
                1,
            ),
            ("self_loop", "self_loop", DEPENDENCY_SELF_LOOP_MESSAGE, 1),
            ("cycle", "cycle", DEPENDENCY_CYCLE_MESSAGE, 1),
        ],
    )
    @pytest.mark.parametrize("json_output", [True, False], ids=["json", "human"])
    def test_batch_deps_hostile_diagnostics_are_bounded_and_redacted(
        self,
        tmp_path: Path,
        case: str,
        expected_code: str,
        expected_message: str,
        human_exit: int,
        json_output: bool,
    ) -> None:
        """Every dependency rejection surface omits attacker-sized task IDs."""
        marker = f"SECRET_{case.upper()}_TASK_ID"
        hostile_id = marker + ("x" * 100_000)
        if case == "malformed":
            seeded = [("T001", [])]
            raw = hostile_id
        elif case == "unknown":
            seeded = [("T001", [])]
            raw = f"{hostile_id}->T001"
        elif case == "self_loop":
            seeded = [(hostile_id, [])]
            raw = f"{hostile_id}->{hostile_id}"
        else:
            seeded = [(hostile_id, []), ("T001", [hostile_id])]
            raw = f"{hostile_id}->T001"

        _seed_dep_tasks(tmp_path, seeded)
        events_path = tmp_path / ".anvil" / "events.jsonl"
        events_before = events_path.read_bytes()
        deps_before = {task_id: _deps_of(tmp_path, task_id) for task_id, _ in seeded}
        args = ["deps", "--add", raw]
        if json_output:
            args.append("--json")

        result = _invoke_cmd(tmp_path, args)

        assert result.exit_code == (1 if json_output else human_exit)
        assert marker not in result.output
        assert len(result.output.encode("utf-8")) <= 4096
        assert events_path.read_bytes() == events_before
        assert {
            task_id: _deps_of(tmp_path, task_id) for task_id, _ in seeded
        } == deps_before
        if json_output:
            assert json.loads(result.output) == {
                "ok": False,
                "command": "deps",
                "error": {
                    "code": expected_code,
                    "message": expected_message,
                },
            }
        else:
            assert result.output.strip() == f"Error: {expected_message}"

    def test_batch_deps_help_documents_options(self) -> None:
        """`deps --help` surfaces options, canonical syntax, and scope caveat."""
        result = runner.invoke(app, ["deps", "--help"])
        assert result.exit_code == 0
        assert "--add" in result.output
        assert "--remove" in result.output
        assert "SOURCE->TARGET" in result.output
        assert "scoped IDs containing ':'" in result.output
        assert "unscoped" in result.output


# ---------------------------------------------------------------------------
# GAP-09 — `review tasks` nudges to approve a still-draft PRD
# ---------------------------------------------------------------------------


class TestReviewTasksDraftPrdHint:
    """GAP-09: after parse + plan + review-tasks the PRD can still be `draft`
    with nothing prompting the user to approve it. `review tasks` emits a
    one-line hint (a nudge, not a hard gate) pointing at `prd review --approve`
    when the PRD is still in draft.
    """

    def test_hint_shown_when_prd_still_draft(self, tmp_path: Path) -> None:
        _do_init(tmp_path, name="Draft Hint Project")
        _write_prd(tmp_path, _COMPLEX_TASK_PRD)
        assert _invoke_cmd(tmp_path, ["prd", "parse"]).exit_code == 0
        # No `prd review` — PRD stays in draft.
        assert _invoke_cmd(tmp_path, ["plan", "--no-llm"]).exit_code == 0
        assert _invoke_cmd(tmp_path, ["score"]).exit_code == 0

        result = _invoke_cmd(tmp_path, ["review", "tasks"])
        assert result.exit_code == 0, result.output
        # The promotion still happened (hint, not a gate).
        assert "Promoted" in result.output
        # The hint points at the approval command.
        assert "draft" in result.output.lower()
        assert "prd review --approve" in result.output

    def test_no_hint_when_prd_approved(self, tmp_path: Path) -> None:
        _do_init(tmp_path, name="Approved Project")
        _write_prd(tmp_path, _COMPLEX_TASK_PRD)
        assert _invoke_cmd(tmp_path, ["prd", "parse"]).exit_code == 0
        assert _invoke_cmd(tmp_path, ["prd", "review"]).exit_code == 0
        assert _invoke_cmd(tmp_path, ["prd", "review", "--approve"]).exit_code == 0
        assert _invoke_cmd(tmp_path, ["plan", "--no-llm"]).exit_code == 0
        assert _invoke_cmd(tmp_path, ["score"]).exit_code == 0

        result = _invoke_cmd(tmp_path, ["review", "tasks"])
        assert result.exit_code == 0, result.output
        assert "prd review --approve" not in result.output


# ---------------------------------------------------------------------------
# SL-6 — `anvil assumptions` ranks PRD requirements by blast x uncertainty
# ---------------------------------------------------------------------------


# A PRD whose requirements span the risk spectrum: R001 is high-blast +
# high-uncertainty (schema + [NEEDS DECISION] + hedging), R002 is concrete and
# low-blast.
_ASSUMPTIONS_PRD = """\
# Project: Assumptions Test

## Summary

Project for the assumptions CLI tests.

## Goals

- Surface risky assumptions early.

## Requirements

- R001: Migrate the database schema somehow [NEEDS DECISION], maybe later.
- R002: Render a list of 10 items, sorted alphabetically.

## Features

### F001: Core

The only feature.

**Requirements:** R001, R002

## Tasks

### T001: Build the thing

**Feature:** F001
**Priority:** medium
**Likely files:** src/a.py

**Acceptance criteria:**

- It works.

**Verification:**

- `pytest -q`

Body.
"""


class TestAssumptionsCommand:
    """SL-6: `anvil assumptions` ranks requirements by blast x uncertainty."""

    def _bootstrap(self, tmp_path: Path) -> None:
        _do_init(tmp_path, name="Assumptions Project")
        _write_prd(tmp_path, _ASSUMPTIONS_PRD)
        assert _invoke_cmd(tmp_path, ["prd", "parse"]).exit_code == 0

    def test_json_envelope_ranks_uncertain_first(self, tmp_path: Path) -> None:
        self._bootstrap(tmp_path)
        result = _invoke_cmd(tmp_path, ["assumptions", "--json"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["ok"] is True
        assert envelope["command"] == "assumptions"
        data = envelope["data"]
        assert data["count"] == 2
        ranked = data["assumptions"]
        # The known-uncertain, high-blast R001 ranks above the concrete R002.
        assert ranked[0]["requirement_id"] == "R001"
        assert ranked[0]["priority"] >= ranked[1]["priority"]
        # Envelope carries the per-dimension breakdown + reasons.
        assert ranked[0]["blast_radius"] >= 1
        assert ranked[0]["uncertainty"] >= 1
        assert ranked[0]["reasons"]

    def test_limit_truncates_ranked_list(self, tmp_path: Path) -> None:
        self._bootstrap(tmp_path)
        result = _invoke_cmd(tmp_path, ["assumptions", "--limit", "1", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["count"] == 1
        assert data["assumptions"][0]["requirement_id"] == "R001"

    def test_human_output_lists_assumptions(self, tmp_path: Path) -> None:
        self._bootstrap(tmp_path)
        result = _invoke_cmd(tmp_path, ["assumptions"])
        assert result.exit_code == 0, result.output
        assert "R001" in result.output
        assert "Priority" in result.output
        assert "why:" in result.output

    def test_empty_prd_exits_zero(self, tmp_path: Path) -> None:
        """A project with no parsed requirements is a friendly no-op, exit 0."""
        _do_init(tmp_path, name="Empty Assumptions Project")
        # No prd parse → requirements table is empty.
        result = _invoke_cmd(tmp_path, ["assumptions"])
        assert result.exit_code == 0, result.output
        assert "No PRD requirements" in result.output

    def test_empty_prd_json_exits_zero(self, tmp_path: Path) -> None:
        _do_init(tmp_path, name="Empty Assumptions JSON Project")
        result = _invoke_cmd(tmp_path, ["assumptions", "--json"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["ok"] is True
        assert envelope["data"]["count"] == 0
        assert envelope["data"]["assumptions"] == []

    def test_is_advisory_never_blocks_claims(self, tmp_path: Path) -> None:
        """The command is read-only: running it does not change task state."""
        self._bootstrap(tmp_path)
        assert _invoke_cmd(tmp_path, ["plan", "--no-llm"]).exit_code == 0
        before = _invoke_cmd(tmp_path, ["list", "--json"]).output
        assert _invoke_cmd(tmp_path, ["assumptions"]).exit_code == 0
        after = _invoke_cmd(tmp_path, ["list", "--json"]).output
        assert before == after

    def test_help_documents_limit(self) -> None:
        result = runner.invoke(app, ["assumptions", "--help"])
        assert result.exit_code == 0
        assert "--limit" in result.output


# ---------------------------------------------------------------------------
# list --open / --summary (PR #170)
# ---------------------------------------------------------------------------


def _seed_status_tasks(tmp_path: Path, ids_with_status: list[tuple[str, str]]) -> None:
    """Seed tasks with explicit statuses via raw SQLite (same idiom as
    ``_seed_dep_tasks``) so the open/terminal split is deterministic.

    Note: the live state machine auto-promotes ``rejected`` → ``drafted`` and
    ``accepted`` → ``done`` in the same transaction; seeding those resting
    states directly models legacy/crashed-loop DBs, which is exactly what
    ``--open`` must classify correctly (rejected = open, accepted = finished).
    """
    _do_init(tmp_path, name="List Test Project")
    db_path = tmp_path / ".anvil" / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR IGNORE INTO features "
        "(id, title, description, status, requirements, tasks) "
        "VALUES ('F001', 'List Feature', 'desc', 'proposed', '[]', '[]')"
    )
    for task_id, task_status in ids_with_status:
        conn.execute(
            """INSERT OR REPLACE INTO tasks
            (id, feature_id, title, description, status, priority, task_type,
             dependencies, conflict_groups, scores, acceptance_criteria,
             implementation_notes, verification, likely_files,
             parent_task_id, created_at, updated_at)
            VALUES (?, 'F001', ?, 'desc', ?, 'medium', 'feature',
             '[]', '[]', '{}', '["x"]', '[]', '{}', '[]',
             NULL, '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00')""",
            (task_id, f"Title {task_id}", task_status),
        )
    conn.commit()
    conn.close()


class TestListOpenAndSummary:
    """PR #170: `--open` (terminal-status filter via the canonical
    ``TERMINAL_TASK_STATUSES``) and `--summary` (per-PRD rollup reusing
    ``compute_prd_rollup``). Rejected is OPEN (awaits rework); Total in
    summary mode is never reduced by ``--open``."""

    SEED = [
        ("T001", "done"),
        ("T002", "accepted"),
        ("T003", "rejected"),
        ("T004", "ready"),
        ("T005", "claimed"),
        ("T006", "needs_review"),
    ]

    def test_open_excludes_only_terminal_statuses(self, tmp_path: Path) -> None:
        _seed_status_tasks(tmp_path, self.SEED)
        result = _invoke_cmd(tmp_path, ["list", "--open", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        # rejected (T003) awaits rework -> open; done/accepted are terminal.
        assert data["count"] == 4
        assert sorted(t["id"] for t in data["tasks"]) == [
            "T003",
            "T004",
            "T005",
            "T006",
        ]

    def test_summary_rolls_up_per_prd(self, tmp_path: Path) -> None:
        _seed_status_tasks(tmp_path, self.SEED)
        result = _invoke_cmd(tmp_path, ["list", "--summary", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["total"] == 6
        assert data["open"] == 4
        (row,) = data["summary"]
        assert row["open"] == 4
        assert row["total"] == 6
        assert row["by_status"]["done"] == 1
        assert "proposed" not in row["by_status"]  # zero counts elided

    def test_open_summary_keeps_true_totals(self, tmp_path: Path) -> None:
        """--open hides fully-terminal PRDs but never shrinks Total."""
        _seed_status_tasks(tmp_path, self.SEED)
        result = _invoke_cmd(tmp_path, ["list", "--open", "--summary"])
        assert result.exit_code == 0, result.output
        assert "1 PRD(s), 4 open of 6 total." in result.output

    def test_open_all_terminal_says_no_open_tasks(self, tmp_path: Path) -> None:
        _seed_status_tasks(tmp_path, [("T001", "done"), ("T002", "accepted")])
        result = _invoke_cmd(tmp_path, ["list", "--open"])
        assert result.exit_code == 0, result.output
        assert "No tasks found (open)." in result.output
        summary = _invoke_cmd(tmp_path, ["list", "--open", "--summary"])
        assert "No open tasks." in summary.output

    def test_help_documents_new_flags(self) -> None:
        result = runner.invoke(app, ["list", "--help"])
        assert result.exit_code == 0
        assert "--open" in result.output
        assert "--summary" in result.output
