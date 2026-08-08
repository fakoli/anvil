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

import base64
import datetime as _dt
import hashlib
import json as _json
import os
import sqlite3 as _sqlite3
import subprocess
from pathlib import Path

import pytest
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

    from anvil.state.models import (
        HookCommandAttribution,
        hook_command_semantic_digest,
    )

    db_path = tmp_path / ".anvil" / "state.db"
    conn = _sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT id, generation, claimed_by, attestation_context "
            "FROM claims WHERE task_id=? AND status='active' LIMIT 1",
            (task_id,),
        ).fetchone()
        project_id = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    assert row is not None, "no active claim to attach a proof to"
    context = _json.loads(row[3])
    captured_at = _dt.datetime.now(_dt.UTC)
    output_sha256 = _hashlib.sha256(b"out").hexdigest()
    attribution = HookCommandAttribution(
        project_id=project_id,
        claim_id=row[0],
        generation=row[1],
        claimed_by=row[2],
        task_id=task_id,
        task_revision=context["task_revision"],
        prd_id=context["prd_id"],
        prd_revision=context["prd_revision"],
        repository_id=context["repository_id"],
        claim_start_sha=context["claim_start_sha"],
    )
    buf = tmp_path / ".anvil" / ".evidence-buffer"
    buf.mkdir(parents=True, exist_ok=True)
    rec = {
        "kind": "command",
        "timestamp": captured_at.isoformat(),
        "command": command,
        "exit_code": exit_code,
        "output_sha256": output_sha256,
        "stdout_excerpt": "out",
        "stderr_excerpt": "",
        "actor": "agent-test",
        "claim_id": row[0],
        "attribution": attribution.model_dump(mode="json"),
        "semantic_digest": hook_command_semantic_digest(
            attribution=attribution,
            command=command,
            exit_code=exit_code,
            output_sha256=output_sha256,
            captured_at=captured_at,
        ),
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


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Anvil Tests"], cwd=root, check=True
    )
    (root / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True
    )


def _write_claim_command_artifact(
    tmp_path: Path, claim: dict[str, object], command: str
) -> Path:
    from anvil.claims.command_proof_artifact import claim_command_cwd_identity
    from anvil.state.hashing import canonical_json_bytes

    conn = _sqlite3.connect(str(tmp_path / ".anvil" / "state.db"))
    created_raw = conn.execute(
        "SELECT created_at FROM claims WHERE id = ?", (claim["id"],)
    ).fetchone()[0]
    project_id = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
    conn.close()
    created = _dt.datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
    ended = _dt.datetime.now(_dt.UTC)
    context = claim["attestation_context"]
    assert isinstance(context, dict)
    output = b"1 passed\n"
    cwd_identity = claim_command_cwd_identity(
        tmp_path,
        context["repository_id"],
        ".",
    )
    payload = {
        "schema_version": 1,
        "project_id": project_id,
        "claim_id": claim["id"],
        "generation": claim["generation"],
        "claimed_by": claim["claimed_by"],
        "task_id": claim["task_id"],
        "task_revision": context["task_revision"],
        "prd_id": context["prd_id"],
        "prd_revision": context["prd_revision"],
        "repository_id": context["repository_id"],
        "claim_start_sha": context["claim_start_sha"],
        "cwd_relative": ".",
        "cwd_identity": cwd_identity,
        "command_base64": base64.b64encode(command.encode()).decode(),
        "started_at": created.astimezone(_dt.UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "ended_at": ended.isoformat().replace("+00:00", "Z"),
        "exit_code": 0,
        "output_base64": base64.b64encode(output).decode(),
        "output_sha256": hashlib.sha256(output).hexdigest(),
    }
    path = tmp_path / "command-proof.json"
    path.write_bytes(
        canonical_json_bytes(
            {"envelope_id": "ENV-CLI-1", "payload": payload},
            max_bytes=262_144,
            max_string_bytes=262_144,
        )
    )
    return path


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

    def test_claim_bound_artifact_satisfies_typed_proof_without_hook(
        self, tmp_path: Path
    ) -> None:
        _init_git_repo(tmp_path)
        task_id = _planned(tmp_path)
        claim_result = _invoke(
            tmp_path, ["claim", task_id, "--actor", "agent-test", "--json"]
        )
        assert claim_result.exit_code == 0, claim_result.output
        claim = _json.loads(claim_result.stdout)["data"]["claim"]
        artifact = _write_claim_command_artifact(
            tmp_path, claim, _PLANNED_VERIFY_CMD
        )

        result = _invoke(
            tmp_path,
            [
                "submit",
                task_id,
                "--commands",
                _PLANNED_VERIFY_CMD,
                "--files-changed",
                "src/app/converter.py",
                "--command-proof-file",
                str(artifact),
                "--actor",
                "agent-test",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = _json.loads(result.stdout)["data"]
        assert data["missing_claim_bound_proofs"] == []
        assert data["legacy_hook_proofs"] == []
        receipt = data["claim_bound_command_proofs"][0]
        assert receipt["command"] == _PLANNED_VERIFY_CMD
        assert receipt["trust_mode"] == "claim_owner_self_attested"

        apply_result = _invoke(
            tmp_path,
            [
                "apply",
                task_id,
                "--approve",
                "--strict",
                "--reviewer",
                "human",
                "--json",
            ],
        )
        assert apply_result.exit_code == 0, apply_result.output
        acceptance_path = _json.loads(apply_result.stdout)["data"]["proof_path"]
        from anvil.state.models import AcceptanceProof, ClaimCommandProof

        acceptance = AcceptanceProof.model_validate_json(
            Path(acceptance_path).read_text(encoding="utf-8")
        )
        assert any(
            isinstance(proof, ClaimCommandProof)
            for proof in acceptance.command_results
        )

    def test_claim_bound_artifact_batch_is_atomic_on_one_invalid_file(
        self, tmp_path: Path
    ) -> None:
        _init_git_repo(tmp_path)
        task_id = _planned(tmp_path)
        claim_result = _invoke(
            tmp_path, ["claim", task_id, "--actor", "agent-test", "--json"]
        )
        claim = _json.loads(claim_result.stdout)["data"]["claim"]
        valid = _write_claim_command_artifact(tmp_path, claim, _PLANNED_VERIFY_CMD)
        invalid = tmp_path / "invalid-proof.json"
        invalid.write_text("{not-json", encoding="utf-8")

        result = _invoke(
            tmp_path,
            [
                "submit",
                task_id,
                "--commands",
                _PLANNED_VERIFY_CMD,
                "--files-changed",
                "src/app/converter.py",
                "--command-proof-file",
                str(valid),
                "--command-proof-file",
                str(invalid),
                "--actor",
                "agent-test",
                "--json",
            ],
        )
        assert result.exit_code == 1
        assert _status(tmp_path, task_id) == "claimed"
        conn = _sqlite3.connect(str(tmp_path / ".anvil" / "state.db"))
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE task_id = ?", (task_id,)
            ).fetchone()[0] == 0
        finally:
            conn.close()

    def test_expiry_during_artifact_load_refuses_without_mutation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anvil.clock as clock_module
        from anvil.claims import command_proof_artifact

        _init_git_repo(tmp_path)
        task_id = _planned(tmp_path)
        claim_result = _invoke(
            tmp_path, ["claim", task_id, "--actor", "agent-test", "--json"]
        )
        claim = _json.loads(claim_result.stdout)["data"]["claim"]
        artifact = _write_claim_command_artifact(
            tmp_path, claim, _PLANNED_VERIFY_CMD
        )
        before_expiry = _dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=1)
        expiry = before_expiry + _dt.timedelta(seconds=1)
        after_expiry = expiry + _dt.timedelta(seconds=1)
        conn = _sqlite3.connect(str(tmp_path / ".anvil" / "state.db"))
        conn.execute(
            "UPDATE claims SET lease_expires_at = ? WHERE id = ?",
            (expiry.isoformat(), claim["id"]),
        )
        conn.commit()
        conn.close()

        class AdvancingClock:
            current = before_expiry

            def now(self) -> _dt.datetime:
                return self.current

        real_loader = command_proof_artifact.load_claim_command_proof

        def load_then_expire(*args, **kwargs):  # type: ignore[no-untyped-def]
            loaded = real_loader(*args, **kwargs)
            AdvancingClock.current = after_expiry
            return loaded

        monkeypatch.setattr(clock_module, "SystemClock", AdvancingClock)
        monkeypatch.setattr(
            command_proof_artifact, "load_claim_command_proof", load_then_expire
        )
        result = _invoke(
            tmp_path,
            [
                "submit",
                task_id,
                "--commands",
                _PLANNED_VERIFY_CMD,
                "--files-changed",
                "src/app/converter.py",
                "--command-proof-file",
                str(artifact),
                "--actor",
                "agent-test",
                "--json",
            ],
        )
        assert result.exit_code == 1
        assert _json.loads(result.stdout)["error"]["code"] == "claim_expired"
        assert _status(tmp_path, task_id) == "claimed"
        conn = _sqlite3.connect(str(tmp_path / ".anvil" / "state.db"))
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE task_id = ?", (task_id,)
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT status FROM claims WHERE id = ?", (claim["id"],)
            ).fetchone()[0] == "active"
        finally:
            conn.close()

    @pytest.mark.parametrize("oversized_kind", ["item_count", "aggregate_bytes"])
    def test_adapter_refuses_oversized_batch_before_loading_or_mutating(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        oversized_kind: str,
    ) -> None:
        from anvil.claims import command_proof_artifact

        task_id = _planned(tmp_path)
        assert _invoke(
            tmp_path, ["claim", task_id, "--actor", "agent-test"]
        ).exit_code == 0

        calls = 0

        def forbidden_loader(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            raise AssertionError("artifact loader must not run")

        monkeypatch.setattr(
            command_proof_artifact, "load_claim_command_proof", forbidden_loader
        )
        if oversized_kind == "item_count":
            paths = [tmp_path / f"missing-{index}.json" for index in range(17)]
        else:
            paths = [tmp_path / "large-a.json", tmp_path / "large-b.json"]
            for path in paths:
                path.write_bytes(b"x" * 600_000)

        command = [
            "submit",
            task_id,
            "--commands",
            _PLANNED_VERIFY_CMD,
            "--files-changed",
            "src/app/converter.py",
        ]
        for path in paths:
            command.extend(["--command-proof-file", str(path)])
        command.extend(["--actor", "agent-test", "--json"])
        result = _invoke(tmp_path, command)

        assert result.exit_code == 1
        assert calls == 0
        assert _status(tmp_path, task_id) == "claimed"
        conn = _sqlite3.connect(str(tmp_path / ".anvil" / "state.db"))
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE task_id = ?", (task_id,)
            ).fetchone()[0] == 0
        finally:
            conn.close()

    def test_output_file_is_descriptive_and_reports_typed_proof_missing(
        self, tmp_path: Path
    ) -> None:
        task_id = _planned(tmp_path)
        assert _invoke(
            tmp_path, ["claim", task_id, "--actor", "agent-test"]
        ).exit_code == 0
        output = tmp_path / "pytest.log"
        output.write_text("exit: 0\nall tests passed\n", encoding="utf-8")

        res = _invoke(
            tmp_path,
            [
                "submit",
                task_id,
                "--commands",
                _PLANNED_VERIFY_CMD,
                "--files-changed",
                "src/app/converter.py",
                "--output-file",
                str(output),
                "--actor",
                "agent-test",
                "--json",
            ],
        )
        assert res.exit_code == 0, res.output
        data = _json.loads(res.stdout)["data"]
        assert data["claim_bound_command_proofs"] == []
        assert data["legacy_hook_proofs"] == []
        assert data["missing_claim_bound_proofs"]
        assert data["missing_legacy_evidence"] == []

    def test_human_submit_distinguishes_typed_proof_from_output_excerpt(
        self, tmp_path: Path
    ) -> None:
        task_id = _planned(tmp_path)
        assert _invoke(
            tmp_path, ["claim", task_id, "--actor", "agent-test"]
        ).exit_code == 0
        output = tmp_path / "pytest.log"
        output.write_text("exit: 0\nall tests passed\n", encoding="utf-8")
        result = _invoke(
            tmp_path,
            [
                "submit",
                task_id,
                "--commands",
                _PLANNED_VERIFY_CMD,
                "--files-changed",
                "src/app/converter.py",
                "--output-file",
                str(output),
                "--actor",
                "agent-test",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Missing typed required_proofs" in result.output
        assert "--output-file is descriptive only" in result.output
        assert "missing items for required_evidence" not in result.output

    def test_hook_capture_attribution_strict_passes_on_zero_exit(
        self, tmp_path: Path
    ) -> None:
        _init_git_repo(tmp_path)
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

    def test_hook_capture_attribution_strict_refuses_nonzero_exit(
        self, tmp_path: Path
    ) -> None:
        """The closed hole: a recorded command that FAILED (exit 1) must not let
        the task through the strict gate, even though it 'ran'."""
        _init_git_repo(tmp_path)
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

    def test_hook_capture_attribution_emits_verifiable_acceptance_proof(
        self, tmp_path: Path
    ) -> None:
        """B48 part 2: accepting a task writes a portable signed AcceptanceProof
        that carries the observed CommandProof and verifies against its signer."""
        from anvil import signing
        from anvil.state.models import AcceptanceProof

        _init_git_repo(tmp_path)
        task_id = _planned(tmp_path)
        assert _invoke(
            tmp_path, ["claim", task_id, "--actor", "agent-test"]
        ).exit_code == 0
        _inject_command_proof(tmp_path, task_id, _PLANNED_VERIFY_CMD, exit_code=0)
        assert _invoke(
            tmp_path,
            [
                "submit", task_id,
                "--commands", _PLANNED_VERIFY_CMD,
                "--files-changed", "src/app/converter.py",
                "--actor", "agent-test",
            ],
        ).exit_code == 0
        res = _invoke(
            tmp_path,
            ["apply", task_id, "--approve", "--strict", "--reviewer", "human", "--json"],
        )
        assert res.exit_code == 0, res.output
        proof_path = _json.loads(res.stdout.strip())["data"]["proof_path"]
        assert proof_path is not None, "acceptance should emit a proof"
        pf = Path(proof_path)
        assert pf.exists()

        proof = AcceptanceProof.model_validate_json(pf.read_text(encoding="utf-8"))
        assert proof.task_id == task_id
        assert proof.project_id, "proof must be bound to its originating project"
        assert any(
            cp.command == _PLANNED_VERIFY_CMD and cp.exit_code == 0
            for cp in proof.command_results
        ), "the proof must carry the observed passing command"
        # verifies against its own signer (trust the embedded signer for the test)
        ok, problems = signing.verify_acceptance(proof, {proof.signer_id})
        assert ok, problems
        # and the CLI verifier accepts it with a matching trust list
        trust = tmp_path / "trust.txt"
        trust.write_text(proof.signer_id + "\n", encoding="utf-8")
        vres = _invoke(
            tmp_path, ["proof", "verify", proof_path, "--trust", str(trust), "--json"]
        )
        assert vres.exit_code == 0, vres.output
