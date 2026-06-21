"""Typed-proof gate + evidence-buffer reconciliation tests (SL-3 / B48).

These lock the non-gameable property: a ``command`` requirement is satisfiable
ONLY by an observed :class:`CommandProof` whose ``exit_code`` is in the passing
set. A free-text :class:`AssertionProof` carrying the command text cannot
impersonate it, and a recorded command that exited non-zero is refused.

The companion ``_read_command_proofs`` tests lock the "observed, not asserted"
data path: the PostToolUse hook writes real exit codes to the per-claim buffer,
and ``anvil submit`` reconciles them into ``Evidence.proofs``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

from anvil.cli.packet_apply import _read_command_proofs
from anvil.review.gates import evidence_complete
from anvil.state.models import (
    AssertionProof,
    CommandProof,
    DiffProof,
    Evidence,
    LinkProof,
    ProofKind,
    ProofRequirement,
    Task,
    Verification,
)

_UTC = datetime.UTC
_NOW = datetime.datetime(2026, 6, 21, 12, 0, 0, tzinfo=_UTC)
_HASH = "a" * 64


def _task(*, required_proofs=(), required_evidence=()) -> Task:
    return Task(
        id="T1",
        feature_id="F1",
        title="t",
        description="d",
        verification=Verification(
            required_proofs=list(required_proofs),
            required_evidence=list(required_evidence),
        ),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _evidence(*, proofs=(), **string_fields) -> Evidence:
    return Evidence(
        id="EV1",
        task_id="T1",
        claim_id="C1",
        proofs=list(proofs),
        submitted_at=_NOW,
        submitted_by="agent",
        **string_fields,
    )


def _cmd_proof(command: str, exit_code: int) -> CommandProof:
    return CommandProof(
        command=command, exit_code=exit_code, output_sha256=_HASH, captured_at=_NOW
    )


# ---------------------------------------------------------------------------
# The non-gameable command requirement
# ---------------------------------------------------------------------------

_PYTEST = "uv run pytest -q"
_CMD_REQ = ProofRequirement(
    kind=ProofKind.command,
    command=_PYTEST,
    passing_exit_codes=[0],
    label="tests pass",
)


def test_passing_command_proof_satisfies_requirement() -> None:
    passed, missing = evidence_complete(
        _task(required_proofs=[_CMD_REQ]),
        _evidence(proofs=[_cmd_proof(_PYTEST, 0)]),
    )
    assert passed is True
    assert missing == []


def test_failed_command_proof_does_not_satisfy() -> None:
    """A recorded command that exited non-zero must NOT satisfy the requirement."""
    passed, missing = evidence_complete(
        _task(required_proofs=[_CMD_REQ]),
        _evidence(proofs=[_cmd_proof(_PYTEST, 1)]),
    )
    assert passed is False
    assert missing == ["tests pass"]


def test_assertion_cannot_impersonate_a_command() -> None:
    """The gameability regression: prose claiming success does not satisfy a
    command requirement."""
    spoof = AssertionProof(statement=f"{_PYTEST} passed", attested_by="agent")
    passed, missing = evidence_complete(
        _task(required_proofs=[_CMD_REQ]),
        _evidence(proofs=[spoof]),
    )
    assert passed is False
    assert missing == ["tests pass"]


def test_command_match_is_exact() -> None:
    """A CommandProof for a different command does not satisfy the requirement."""
    passed, _ = evidence_complete(
        _task(required_proofs=[_CMD_REQ]),
        _evidence(proofs=[_cmd_proof("uv run pytest other/", 0)]),
    )
    assert passed is False


def test_custom_passing_exit_codes() -> None:
    """passing_exit_codes can admit non-zero codes (e.g. 'no tests collected')."""
    req = ProofRequirement(
        kind=ProofKind.command,
        command=_PYTEST,
        passing_exit_codes=[0, 5],
        label="tests pass or empty",
    )
    passed, _ = evidence_complete(
        _task(required_proofs=[req]), _evidence(proofs=[_cmd_proof(_PYTEST, 5)])
    )
    assert passed is True


def test_link_requirement_with_substring() -> None:
    req = ProofRequirement(
        kind=ProofKind.link, link_contains="/pull/", label="PR link"
    )
    ok_ev = _evidence(proofs=[LinkProof(url="https://gh/x/pull/12")])
    bad_ev = _evidence(proofs=[LinkProof(url="https://gh/x/issues/12")])
    assert evidence_complete(_task(required_proofs=[req]), ok_ev)[0] is True
    assert evidence_complete(_task(required_proofs=[req]), bad_ev)[0] is False


def test_diff_requirement() -> None:
    req = ProofRequirement(kind=ProofKind.diff, label="a diff exists")
    ev = _evidence(proofs=[DiffProof(diff_sha256=_HASH, files_changed=["a.py"])])
    assert evidence_complete(_task(required_proofs=[req]), ev)[0] is True
    assert evidence_complete(_task(required_proofs=[req]), _evidence())[0] is False


def test_no_requirements_is_a_noop() -> None:
    assert evidence_complete(_task(), _evidence()) == (True, [])


def test_legacy_and_typed_paths_both_enforced() -> None:
    """required_evidence (legacy substring) AND required_proofs (typed) must
    both be satisfied; an unmet item from either surface fails the gate."""
    task = _task(required_proofs=[_CMD_REQ], required_evidence=["screenshots"])
    # typed satisfied, legacy missing -> fail, missing names the legacy item
    passed, missing = evidence_complete(
        task, _evidence(proofs=[_cmd_proof(_PYTEST, 0)])
    )
    assert passed is False
    assert "screenshots" in missing
    # both satisfied -> pass
    passed, missing = evidence_complete(
        task,
        _evidence(proofs=[_cmd_proof(_PYTEST, 0)], screenshots=["shot.png"]),
    )
    assert passed is True


def test_missing_items_are_deduplicated_across_both_surfaces() -> None:
    """A legacy required_evidence string and a typed required_proofs label can
    coincide; the missing list reports it once, not twice."""
    req = ProofRequirement(kind=ProofKind.command, command="x", label="run tests")
    task = _task(required_proofs=[req], required_evidence=["run tests"])
    passed, missing = evidence_complete(task, _evidence())
    assert passed is False
    assert missing == ["run tests"]  # deduped, not ["run tests", "run tests"]


def test_command_requirement_without_command_is_rejected_at_construction() -> None:
    """A command-kind ProofRequirement with no command can never be satisfied,
    so it is refused at construction rather than failing silently."""
    import pytest

    with pytest.raises(ValueError, match="requires `command`"):
        ProofRequirement(kind=ProofKind.command, label="bad")


# ---------------------------------------------------------------------------
# Evidence-buffer reconciliation (hook -> submit)
# ---------------------------------------------------------------------------


def _write_buffer(state_dir: Path, claim_id: str, records: list) -> None:
    buf = state_dir / ".evidence-buffer"
    buf.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) if isinstance(r, dict) else r for r in records]
    (buf / f"{claim_id}.json").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _command_record(command: str, exit_code: int) -> dict:
    return {
        "kind": "command",
        "timestamp": _NOW.isoformat(),
        "command": command,
        "exit_code": exit_code,
        "output_sha256": hashlib.sha256(b"out").hexdigest(),
        "stdout_excerpt": "out",
        "stderr_excerpt": "",
        "actor": "agent",
    }


def test_read_command_proofs_parses_valid_records(tmp_path: Path) -> None:
    _write_buffer(
        tmp_path,
        "C1",
        [_command_record("uv run pytest -q", 0), _command_record("make build", 2)],
    )
    proofs = _read_command_proofs(tmp_path, "C1")
    assert [(p.command, p.exit_code) for p in proofs] == [
        ("uv run pytest -q", 0),
        ("make build", 2),
    ]
    assert all(isinstance(p, CommandProof) for p in proofs)


def test_read_command_proofs_skips_partial_and_malformed(tmp_path: Path) -> None:
    """A pre-SL-3 record (no output_sha256) or a torn line is skipped, never
    fatal — submit must still succeed."""
    partial = {
        "command": "old",
        "exit_code": 0,
        "timestamp": _NOW.isoformat(),
    }  # no output_sha256
    _write_buffer(
        tmp_path,
        "C1",
        [_command_record("uv run pytest -q", 0), partial, "{not json"],
    )
    proofs = _read_command_proofs(tmp_path, "C1")
    assert len(proofs) == 1
    assert proofs[0].command == "uv run pytest -q"


def test_read_command_proofs_missing_buffer_is_empty(tmp_path: Path) -> None:
    assert _read_command_proofs(tmp_path, "NOPE") == []
