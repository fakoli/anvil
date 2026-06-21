"""Strict completion-evidence enforcement tests (T025/B25).

The evidence gate (``review.gates.evidence_complete``) checks submitted
evidence against a task's ``Verification.required_evidence``. By default the
gate is ADVISORY — ``apply --approve`` shows the verdict but transitions to
done regardless. This module verifies the CONFIGURABLE STRICT MODE:

* (a) insufficient evidence + ``--strict`` (and + ``strict_evidence`` config)
      → apply --approve REFUSES: task NOT done, exit nonzero, missing reported;
* (b) sufficient evidence + strict → apply --approve proceeds → done;
* (c) DEFAULT (no flag, no config) + insufficient evidence → still done
      (advisory behaviour preserved byte-for-byte);
* (d) ``--json`` strict rejection → {"ok": false, ..., "error":
      {"code": "evidence_incomplete", "missing": [...]}} + exit 1.

Pattern mirrors ``tests/test_cli.py`` (Typer ``CliRunner`` + ``os.chdir`` into
a per-test ``tmp_path``, direct-DB mutation to inject ``required_evidence``
since the planner does not surface it — same technique as
``test_submit_with_screenshots_records_them``).
"""

from __future__ import annotations

import json as _json
import os
import sqlite3 as _sqlite3
from pathlib import Path

from typer.testing import CliRunner

from anvil.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# A minimal PRD that yields at least one ready task via the deterministic
# --no-llm plan path (mirrors tests/test_json_output.py::_FULL_PRD).
# ---------------------------------------------------------------------------
_PRD = """\
# Project: Strict Evidence Test Project

## Summary

A project for strict completion-evidence enforcement testing.

## Goals

- Convert files correctly.

## Requirements

- R001: Accept file input.

## Acceptance Criteria

- Converts files correctly.

## Features

### F001: File Conversion

Convert input files to output format.

**Requirements:** R001

## Tasks

### T001: Implement converter

**Feature:** F001
**Priority:** high
**Likely files:** src/app/converter.py

**Acceptance criteria:**

- Conversion succeeds for valid input.

**Verification:**

- `pytest tests/test_converter.py -v`
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke(tmp_path: Path, cmd: list[str]):  # type: ignore[no-untyped-def]
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        return runner.invoke(app, cmd, catch_exceptions=False)
    finally:
        os.chdir(original_cwd)


def _planned(tmp_path: Path) -> str:
    """init → PRD → review → approve → plan --no-llm → score → review tasks.

    Returns the first ready task id.
    """
    assert _invoke(
        tmp_path, ["init", "--name", "Strict Evidence Test Project"]
    ).exit_code == 0
    (tmp_path / ".anvil" / "prd.md").write_text(_PRD, encoding="utf-8")
    assert _invoke(tmp_path, ["prd", "parse"]).exit_code == 0
    assert _invoke(tmp_path, ["prd", "review"]).exit_code == 0
    assert _invoke(tmp_path, ["prd", "review", "--approve"]).exit_code == 0
    assert _invoke(tmp_path, ["plan", "--no-llm"]).exit_code == 0
    assert _invoke(tmp_path, ["score"]).exit_code == 0
    assert _invoke(tmp_path, ["review", "tasks"]).exit_code == 0

    db_path = tmp_path / ".anvil" / "state.db"
    conn = _sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT id FROM tasks WHERE status='ready' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "no ready task after planning"
    return row[0]


def _require_screenshots_evidence(tmp_path: Path, task_id: str) -> None:
    """Inject required_evidence=['screenshots'] into the task's verification.

    Same direct-DB mutation as test_cli.py's screenshot-gate tests — the
    planner does not surface required_evidence today.
    """
    db_path = tmp_path / ".anvil" / "state.db"
    conn = _sqlite3.connect(str(db_path))
    try:
        verification_json = _json.dumps(
            {
                "commands": ["pytest tests/ -v"],
                "manual_steps": [],
                "required_evidence": ["screenshots"],
            }
        )
        conn.execute(
            "UPDATE tasks SET verification = ? WHERE id = ?",
            (verification_json, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_requirements(tmp_path: Path, task_id: str) -> None:
    """Wipe a task's verification so it declares NO requirements at all.

    The planner now emits typed ``required_proofs`` from the verification
    commands (SL-3 / B48), so a raw planned task is no longer requirement-free.
    Tests that want the genuine "nothing to satisfy" gate no-op clear them here.
    """
    db_path = tmp_path / ".anvil" / "state.db"
    conn = _sqlite3.connect(str(db_path))
    try:
        verification_json = _json.dumps(
            {
                "commands": [],
                "manual_steps": [],
                "required_evidence": [],
                "required_proofs": [],
            }
        )
        conn.execute(
            "UPDATE tasks SET verification = ? WHERE id = ?",
            (verification_json, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _inject_command_proof(
    tmp_path: Path, task_id: str, command: str, exit_code: int
) -> None:
    """Simulate the PostToolUse hook: write a CommandProof to the active claim's
    evidence buffer so ``anvil submit`` reconciles it into Evidence.proofs."""
    import datetime as _dt
    import hashlib as _hashlib

    db_path = tmp_path / ".anvil" / "state.db"
    conn = _sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT id FROM claims WHERE task_id=? AND status='active' LIMIT 1",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "no active claim to attach a proof to"
    buf = tmp_path / ".anvil" / ".evidence-buffer"
    buf.mkdir(parents=True, exist_ok=True)
    rec = {
        "kind": "command",
        "timestamp": _dt.datetime.now(_dt.UTC).isoformat(),
        "command": command,
        "exit_code": exit_code,
        "output_sha256": _hashlib.sha256(b"out").hexdigest(),
        "stdout_excerpt": "out",
        "stderr_excerpt": "",
        "actor": "agent-test",
    }
    (buf / f"{row[0]}.json").write_text(_json.dumps(rec) + "\n", encoding="utf-8")


def _status(tmp_path: Path, task_id: str) -> str | None:
    db_path = tmp_path / ".anvil" / "state.db"
    conn = _sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _reach_needs_review_insufficient(tmp_path: Path, task_id: str) -> None:
    """claim + submit WITHOUT --screenshots → needs_review, gate INCOMPLETE."""
    assert _invoke(
        tmp_path, ["claim", task_id, "--actor", "agent-test"]
    ).exit_code == 0
    res = _invoke(
        tmp_path,
        [
            "submit",
            task_id,
            "--commands",
            "pytest tests/ -v",
            "--files-changed",
            "src/app/converter.py",
            "--actor",
            "agent-test",
        ],
    )
    assert res.exit_code == 0, res.output
    assert _status(tmp_path, task_id) == "needs_review"


def _reach_needs_review_sufficient(tmp_path: Path, task_id: str) -> None:
    """claim + submit WITH --screenshots → needs_review, gate PASSED."""
    assert _invoke(
        tmp_path, ["claim", task_id, "--actor", "agent-test"]
    ).exit_code == 0
    res = _invoke(
        tmp_path,
        [
            "submit",
            task_id,
            "--commands",
            "pytest tests/ -v",
            "--files-changed",
            "src/app/converter.py",
            "--screenshots",
            "before.png,after.png",
            "--actor",
            "agent-test",
        ],
    )
    assert res.exit_code == 0, res.output
    assert _status(tmp_path, task_id) == "needs_review"


def _set_config_strict(tmp_path: Path, value: bool) -> None:
    """Append/replace strict_evidence in config.yaml."""
    cfg = tmp_path / ".anvil" / "config.yaml"
    text = cfg.read_text(encoding="utf-8")
    text += f"\nstrict_evidence: {'true' if value else 'false'}\n"
    cfg.write_text(text, encoding="utf-8")


# ===========================================================================
# (a) Strict + insufficient → REFUSE (flag, then config)
# ===========================================================================


class TestStrictRefusesInsufficient:
    def test_strict_flag_refuses_and_task_not_done(self, tmp_path: Path) -> None:
        task_id = _planned(tmp_path)
        _require_screenshots_evidence(tmp_path, task_id)
        _reach_needs_review_insufficient(tmp_path, task_id)

        res = _invoke(
            tmp_path,
            ["apply", task_id, "--approve", "--strict", "--reviewer", "human"],
        )
        # Refused: non-zero exit, task remains needs_review (NOT done).
        assert res.exit_code != 0, res.output
        assert _status(tmp_path, task_id) == "needs_review"
        # Missing item reported on stderr.
        combined = res.output + (
            res.stderr if hasattr(res, "stderr") and res.stderr else ""
        )
        assert "screenshots" in combined

    def test_config_strict_refuses_and_task_not_done(self, tmp_path: Path) -> None:
        task_id = _planned(tmp_path)
        _require_screenshots_evidence(tmp_path, task_id)
        _set_config_strict(tmp_path, True)
        _reach_needs_review_insufficient(tmp_path, task_id)

        # No flag — config strict_evidence: true drives the refusal.
        res = _invoke(
            tmp_path,
            ["apply", task_id, "--approve", "--reviewer", "human"],
        )
        assert res.exit_code != 0, res.output
        assert _status(tmp_path, task_id) == "needs_review"


# ===========================================================================
# (b) Strict + sufficient → PROCEEDS to done
# ===========================================================================


class TestStrictAllowsSufficient:
    def test_strict_flag_with_sufficient_evidence_done(
        self, tmp_path: Path
    ) -> None:
        task_id = _planned(tmp_path)
        _require_screenshots_evidence(tmp_path, task_id)
        _reach_needs_review_sufficient(tmp_path, task_id)

        res = _invoke(
            tmp_path,
            ["apply", task_id, "--approve", "--strict", "--reviewer", "human"],
        )
        assert res.exit_code == 0, res.output
        assert _status(tmp_path, task_id) == "done"

    def test_strict_no_required_evidence_is_noop(self, tmp_path: Path) -> None:
        """A task with NO requirements at all: strict is a no-op → done."""
        task_id = _planned(tmp_path)
        # The planner now emits typed required_proofs from the verification
        # commands; clear ALL requirements so this exercises the genuine
        # "nothing to satisfy" no-op path.
        _clear_requirements(tmp_path, task_id)
        assert _invoke(
            tmp_path, ["claim", task_id, "--actor", "agent-test"]
        ).exit_code == 0
        assert _invoke(
            tmp_path,
            [
                "submit",
                task_id,
                "--commands",
                "pytest tests/ -v",
                "--files-changed",
                "src/app/converter.py",
                "--actor",
                "agent-test",
            ],
        ).exit_code == 0
        res = _invoke(
            tmp_path,
            ["apply", task_id, "--approve", "--strict", "--reviewer", "human"],
        )
        assert res.exit_code == 0, res.output
        assert _status(tmp_path, task_id) == "done"


# ===========================================================================
# (c) DEFAULT (advisory) — insufficient evidence still approves
# ===========================================================================


class TestAdvisoryDefaultPreserved:
    def test_default_no_strict_approves_insufficient(
        self, tmp_path: Path
    ) -> None:
        """Back-compat: no flag, no config → apply --approve still → done."""
        task_id = _planned(tmp_path)
        _require_screenshots_evidence(tmp_path, task_id)
        _reach_needs_review_insufficient(tmp_path, task_id)

        res = _invoke(
            tmp_path,
            ["apply", task_id, "--approve", "--reviewer", "human"],
        )
        assert res.exit_code == 0, res.output
        assert _status(tmp_path, task_id) == "done"

    def test_no_strict_flag_overrides_config_strict(
        self, tmp_path: Path
    ) -> None:
        """--no-strict beats config strict_evidence: true (flag > config)."""
        task_id = _planned(tmp_path)
        _require_screenshots_evidence(tmp_path, task_id)
        _set_config_strict(tmp_path, True)
        _reach_needs_review_insufficient(tmp_path, task_id)

        res = _invoke(
            tmp_path,
            [
                "apply",
                task_id,
                "--approve",
                "--no-strict",
                "--reviewer",
                "human",
            ],
        )
        assert res.exit_code == 0, res.output
        assert _status(tmp_path, task_id) == "done"


# ===========================================================================
# (d) --json strict rejection envelope
# ===========================================================================


class TestStrictJsonRejection:
    def test_json_strict_rejection_envelope(self, tmp_path: Path) -> None:
        task_id = _planned(tmp_path)
        _require_screenshots_evidence(tmp_path, task_id)
        _reach_needs_review_insufficient(tmp_path, task_id)

        res = _invoke(
            tmp_path,
            ["apply", task_id, "--approve", "--strict", "--json"],
        )
        assert res.exit_code == 1, res.output
        envelope = _json.loads(res.stdout.strip())
        assert envelope["ok"] is False
        assert envelope["command"] == "apply"
        assert envelope["error"]["code"] == "evidence_incomplete"
        assert "screenshots" in envelope["error"]["missing"]
        assert "data" not in envelope
        # Task untouched.
        assert _status(tmp_path, task_id) == "needs_review"

    def test_json_strict_reject_flag_still_works(self, tmp_path: Path) -> None:
        """--reject is never gated by strict: --reject --strict succeeds."""
        task_id = _planned(tmp_path)
        _require_screenshots_evidence(tmp_path, task_id)
        _reach_needs_review_insufficient(tmp_path, task_id)

        res = _invoke(
            tmp_path,
            [
                "apply",
                task_id,
                "--reject",
                "--strict",
                "--reason",
                "missing screenshots",
                "--json",
            ],
        )
        assert res.exit_code == 0, res.output
        envelope = _json.loads(res.stdout.strip())
        assert envelope["ok"] is True
        assert envelope["command"] == "apply"
        assert envelope["data"]["decision"] == "rejected"


# ===========================================================================
# (e) SL-3 / B48 — typed CommandProof gate, end to end
# ===========================================================================


# The single verification command the _PRD declares; the planner turns it into a
# typed command ProofRequirement, so the gate now demands an observed exit-0
# CommandProof for exactly this command.
_PLANNED_VERIFY_CMD = "pytest tests/test_converter.py -v"


class TestTypedProofGateEndToEnd:
    """The full observed-proof chain: planner emits required_proofs → hook
    buffers a CommandProof → submit reconciles it → strict apply enforces it."""

    def test_strict_passes_when_observed_command_exited_zero(
        self, tmp_path: Path
    ) -> None:
        task_id = _planned(tmp_path)
        assert _invoke(
            tmp_path, ["claim", task_id, "--actor", "agent-test"]
        ).exit_code == 0
        _inject_command_proof(tmp_path, task_id, _PLANNED_VERIFY_CMD, exit_code=0)
        assert _invoke(
            tmp_path,
            [
                "submit",
                task_id,
                "--commands",
                _PLANNED_VERIFY_CMD,
                "--files-changed",
                "src/app/converter.py",
                "--actor",
                "agent-test",
            ],
        ).exit_code == 0
        res = _invoke(
            tmp_path,
            ["apply", task_id, "--approve", "--strict", "--reviewer", "human"],
        )
        assert res.exit_code == 0, res.output
        assert _status(tmp_path, task_id) == "done"

    def test_strict_refuses_when_observed_command_exited_nonzero(
        self, tmp_path: Path
    ) -> None:
        """The closed hole: a recorded command that FAILED (exit 1) must not let
        the task through the strict gate, even though it 'ran'."""
        task_id = _planned(tmp_path)
        assert _invoke(
            tmp_path, ["claim", task_id, "--actor", "agent-test"]
        ).exit_code == 0
        _inject_command_proof(tmp_path, task_id, _PLANNED_VERIFY_CMD, exit_code=1)
        assert _invoke(
            tmp_path,
            [
                "submit",
                task_id,
                "--commands",
                _PLANNED_VERIFY_CMD,
                "--files-changed",
                "src/app/converter.py",
                "--actor",
                "agent-test",
            ],
        ).exit_code == 0
        res = _invoke(
            tmp_path,
            ["apply", task_id, "--approve", "--strict", "--reviewer", "human"],
        )
        assert res.exit_code != 0
        assert _status(tmp_path, task_id) == "needs_review"
