"""The shipped ``hook dispatch capture-evidence`` path, exercised without mocks.

The dispatcher used to call ``hook_capture_evidence`` without ``output_sha256``.
The Typer ``OptionInfo`` default then reached the digest check, raised, and was
swallowed — so the wired hook recorded nothing while every dispatcher test
(which replaced ``hook_capture_evidence`` with a fake) kept passing. These
tests drive the real command end to end and pin what it persists.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from anvil.claims.evidence_import import inspect_command_buffer
from anvil.cli import app
from anvil.cli import hooks as hooks_mod
from anvil.state.models import ProofKind, ProofRequirement, hook_command_semantic_digest
from tests.test_strict_evidence import _PLANNED_VERIFY_CMD, _invoke, _planned

runner = CliRunner()

_SECRET = "hunter2-do-not-store"


def _dispatch(project: Path, payload: dict[str, object]) -> None:
    result = runner.invoke(
        app,
        ["hook", "dispatch", "capture-evidence", "--cwd", str(project)],
        input=json.dumps(payload),
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output


def _payload(command: str, *, stdout: str = "ok", stderr: str = "", exit_code: int = 0):
    # The PostToolUse(Bash) shape the dispatcher reads.
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "session_id": "sess-capture",
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
    }


def _initialized(tmp_path: Path) -> Path:
    assert _invoke(tmp_path, ["init", "--name", "Capture Dispatch Project"]).exit_code == 0
    return tmp_path / ".anvil"


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _claimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    task = _planned(tmp_path)
    result = _invoke(tmp_path, ["claim", task, "--actor", "agent-alpha", "--json"])
    assert result.exit_code == 0, result.output
    claim = json.loads(result.output)["data"]["claim"]["id"]
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    monkeypatch.setenv("ANVIL_ACTOR", "agent-alpha")
    return tmp_path / ".anvil", claim


def _digest_verifies(proof) -> bool:  # type: ignore[no-untyped-def]
    return proof.semantic_digest == hook_command_semantic_digest(
        attribution=proof.attribution,
        command=proof.command,
        exit_code=proof.exit_code,
        output_sha256=proof.output_sha256,
        captured_at=proof.captured_at,
    )


def test_dispatcher_records_an_orphan_line_without_a_claim(tmp_path: Path) -> None:
    state = _initialized(tmp_path)

    _dispatch(tmp_path, _payload("pytest -q", stdout="3 passed", stderr="warn", exit_code=1))

    (record,) = _lines(state / ".evidence-buffer" / "orphan.json")
    assert record["kind"] == "command"
    assert record["command"] == "pytest -q"
    assert record["exit_code"] == 1
    assert record["stdout_excerpt"] == "3 passed"
    assert record["stderr_excerpt"] == "warn"
    assert record["actor"] == "sess-capture"
    # Hash is over the full raw output (stdout + stderr), not the excerpts.
    import hashlib

    assert record["output_sha256"] == hashlib.sha256(b"3 passedwarn").hexdigest()


def test_dispatcher_records_a_claim_bound_proof_that_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, claim = _claimed(tmp_path, monkeypatch)

    _dispatch(tmp_path, _payload(_PLANNED_VERIFY_CMD, stdout="1 passed"))

    (record,) = _lines(state / ".evidence-buffer" / f"{claim}.json")
    assert record["claim_id"] == claim
    assert record["command"] == _PLANNED_VERIFY_CMD
    (proof,) = inspect_command_buffer(state, claim).proofs
    assert proof.command == _PLANNED_VERIFY_CMD and proof.exit_code == 0
    assert _digest_verifies(proof)


def test_secrets_are_redacted_before_anything_is_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, claim = _claimed(tmp_path, monkeypatch)
    command = f"API_TOKEN={_SECRET} {_PLANNED_VERIFY_CMD}"
    stdout = f"connecting to postgres://app:{_SECRET}@db/x\n1 passed"
    stderr = f"Authorization: Bearer {_SECRET}"

    _dispatch(tmp_path, _payload(command, stdout=stdout, stderr=stderr))

    buffer_file = state / ".evidence-buffer" / f"{claim}.json"
    assert _SECRET not in buffer_file.read_text(encoding="utf-8")
    (record,) = _lines(buffer_file)
    assert record["command"] == f"API_TOKEN=[REDACTED] {_PLANNED_VERIFY_CMD}"
    assert record["stdout_excerpt"] == "connecting to postgres://app:[REDACTED]@db/x\n1 passed"
    assert record["stderr_excerpt"] == "Authorization: Bearer [REDACTED]"
    # The digest binds the stored (redacted) command, so the line still verifies,
    # and the output hash still identifies the raw run.
    import hashlib

    assert record["output_sha256"] == hashlib.sha256((stdout + stderr).encode()).hexdigest()
    (proof,) = inspect_command_buffer(state, claim).proofs
    assert _digest_verifies(proof)


def test_secret_straddling_the_excerpt_boundary_is_not_half_stored(tmp_path: Path) -> None:
    state = _initialized(tmp_path)
    stdout = "x" * 3990 + f" password={_SECRET}-{'z' * 40} tail"

    _dispatch(tmp_path, _payload("pytest -q", stdout=stdout))

    (record,) = _lines(state / ".evidence-buffer" / "orphan.json")
    excerpt = record["stdout_excerpt"]
    assert isinstance(excerpt, str) and len(excerpt) <= 4000
    assert _SECRET[:6] not in excerpt


def test_pinned_required_proof_command_is_kept_verbatim() -> None:
    pinned = "API_TOKEN=fixture pytest tests/test_converter.py -v"
    task = SimpleNamespace(
        required_proofs=[
            ProofRequirement(kind=ProofKind.assertion, label="note"),
            ProofRequirement(kind=ProofKind.command, command=pinned, label="tests"),
        ]
    )
    assert hooks_mod._is_pinned_proof_command(task, pinned) is True
    assert hooks_mod._is_pinned_proof_command(task, pinned + " -x") is False
    assert hooks_mod._is_pinned_proof_command(SimpleNamespace(required_proofs=[]), pinned) is False
    assert hooks_mod._is_pinned_proof_command(None, pinned) is False


def test_capture_evidence_false_records_nothing(tmp_path: Path) -> None:
    state = _initialized(tmp_path)
    config = state / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "capture_evidence: true", "capture_evidence: false"
        ),
        encoding="utf-8",
    )
    assert "capture_evidence: false" in config.read_text(encoding="utf-8")

    _dispatch(tmp_path, _payload("pytest -q"))

    assert not (state / ".evidence-buffer").exists()


def test_unreadable_config_does_not_disable_capture(tmp_path: Path) -> None:
    state = _initialized(tmp_path)
    (state / "config.yaml").write_text("capture_evidence: [not, a, bool\n", encoding="utf-8")

    _dispatch(tmp_path, _payload("pytest -q"))

    assert len(_lines(state / ".evidence-buffer" / "orphan.json")) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_evidence_buffer_directory_is_owner_only(tmp_path: Path) -> None:
    state = _initialized(tmp_path)
    buffer_dir = state / ".evidence-buffer"

    _dispatch(tmp_path, _payload("pytest -q"))
    assert stat.S_IMODE(buffer_dir.stat().st_mode) == 0o700

    # A directory created by an older release (default umask) is tightened.
    os.chmod(buffer_dir, 0o755)
    _dispatch(tmp_path, _payload("pytest -q"))
    assert stat.S_IMODE(buffer_dir.stat().st_mode) == 0o700
    assert len(_lines(buffer_dir / "orphan.json")) == 2
    assert stat.S_IMODE((buffer_dir / "orphan.json").stat().st_mode) == 0o600
