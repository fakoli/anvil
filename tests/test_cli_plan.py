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
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.planning.llm import LLMResponse

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
    """A batch of edges applies atomically (T022 acceptance: 10 edges at once)."""

    def test_batch_deps_applies_ten_edges_atomically(self, tmp_path: Path) -> None:
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
        """The 'SOURCE->TARGET' arrow form is accepted as well as the colon."""
        _seed_dep_tasks(tmp_path, [("T001", []), ("T002", [])])
        result = _invoke_cmd(
            tmp_path, ["deps", "--add", "T002->T001", "--json"]
        )
        assert result.exit_code == 0, result.output
        assert _deps_of(tmp_path, "T002") == ["T001"]

    def test_batch_deps_help_documents_options(self) -> None:
        """`deps --help` surfaces --add and --remove."""
        result = runner.invoke(app, ["deps", "--help"])
        assert result.exit_code == 0
        assert "--add" in result.output
        assert "--remove" in result.output


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
# list --open / --summary
# ---------------------------------------------------------------------------


def _seed_status_tasks(tmp_path: Path, ids_with_status: list[tuple[str, str]]) -> None:
    """Seed tasks with explicit statuses via raw SQLite (same idiom as
    ``_seed_dep_tasks``) so the open/terminal split is deterministic."""
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
    SEED = [
        ("T001", "done"),
        ("T002", "accepted"),
        ("T003", "rejected"),
        ("T004", "ready"),
        ("T005", "claimed"),
        ("T006", "needs_review"),
    ]

    def test_open_excludes_terminal_statuses(self, tmp_path: Path) -> None:
        _seed_status_tasks(tmp_path, self.SEED)
        result = _invoke_cmd(tmp_path, ["list", "--open", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["count"] == 3
        assert sorted(t["id"] for t in data["tasks"]) == ["T004", "T005", "T006"]

    def test_summary_rolls_up_per_prd(self, tmp_path: Path) -> None:
        _seed_status_tasks(tmp_path, self.SEED)
        result = _invoke_cmd(tmp_path, ["list", "--summary", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["total"] == 6
        assert data["open"] == 3
        (row,) = data["summary"]
        assert row["open"] == 3
        assert row["total"] == 6
        assert row["by_status"]["done"] == 1

    def test_summary_human_output(self, tmp_path: Path) -> None:
        _seed_status_tasks(tmp_path, self.SEED)
        result = _invoke_cmd(tmp_path, ["list", "--open", "--summary"])
        assert result.exit_code == 0, result.output
        assert "1 PRD(s), 3 open of 3 total." in result.output

    def test_help_documents_new_flags(self) -> None:
        result = runner.invoke(app, ["list", "--help"])
        assert result.exit_code == 0
        assert "--open" in result.output
        assert "--summary" in result.output
