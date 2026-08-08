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

import base64
import datetime
import hashlib
import json
from pathlib import Path

from anvil.cli.packet_apply import _read_command_proofs
from anvil.review.gates import evidence_complete, evidence_missing_details
from anvil.state.models import (
    AssertionProof,
    ClaimCommandProof,
    CommandProof,
    DiffProof,
    Evidence,
    HookCommandAttribution,
    LinkProof,
    ProofKind,
    ProofRequirement,
    Task,
    Verification,
    hook_command_semantic_digest,
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


def _claim_cmd_proof(command: str) -> ClaimCommandProof:
    command_b64 = base64.b64encode(command.encode()).decode()
    output_b64 = base64.b64encode(b"passed\n").decode()
    output_hash = hashlib.sha256(b"passed\n").hexdigest()
    return ClaimCommandProof.model_validate(
        {
            "kind": "claim_command",
            "command": command,
            "exit_code": 0,
            "output_sha256": output_hash,
            "captured_at": _NOW.isoformat(),
            "semantic_digest": "b" * 64,
            "trust_mode": "claim_owner_self_attested",
            "issuer_id": None,
            "evidence_core": {
                "schema_version": 1,
                "project_id": "P1",
                "claim_id": "C1",
                "generation": 1,
                "claimed_by": "agent",
                "task_id": "T1",
                "task_revision": "c" * 64,
                "prd_id": "default",
                "prd_revision": 1,
                "repository_id": "d" * 64,
                "claim_start_sha": "e" * 40,
                "cwd_relative": ".",
                "cwd_identity": "f" * 64,
                "command_base64": command_b64,
                "started_at": (_NOW - datetime.timedelta(seconds=1))
                .isoformat()
                .replace("+00:00", "Z"),
                "ended_at": _NOW.isoformat().replace("+00:00", "Z"),
                "exit_code": 0,
                "output_base64": output_b64,
                "output_sha256": output_hash,
            },
            "issuer": None,
        }
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


def test_claim_bound_command_proof_satisfies_requirement() -> None:
    passed, missing = evidence_complete(
        _task(required_proofs=[_CMD_REQ]),
        _evidence(proofs=[_claim_cmd_proof(_PYTEST)]),
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


def test_missing_details_keep_descriptive_and_typed_gaps_separate() -> None:
    task = _task(required_proofs=[_CMD_REQ], required_evidence=["screenshots"])
    legacy, typed = evidence_missing_details(task, _evidence())
    assert legacy == ["screenshots"]
    assert typed == ["tests pass"]


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


def _command_record(command: str, exit_code: int, *, claim_id: str = "C1") -> dict:
    output_sha256 = hashlib.sha256(b"out").hexdigest()
    attribution = HookCommandAttribution(
        project_id="P1",
        claim_id=claim_id,
        generation=1,
        claimed_by="agent",
        task_id="T1",
        task_revision="a" * 64,
        prd_id="PRD-1",
        prd_revision=1,
        repository_id="b" * 64,
        claim_start_sha="c" * 40,
    )
    semantic_digest = hook_command_semantic_digest(
        attribution=attribution,
        command=command,
        exit_code=exit_code,
        output_sha256=output_sha256,
        captured_at=_NOW,
    )
    return {
        "kind": "command",
        "timestamp": _NOW.isoformat(),
        "command": command,
        "exit_code": exit_code,
        "output_sha256": output_sha256,
        "stdout_excerpt": "out",
        "stderr_excerpt": "",
        "actor": "agent",
        "claim_id": claim_id,
        "attribution": attribution.model_dump(mode="json"),
        "semantic_digest": semantic_digest,
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
    """Historical unattributed and torn lines are skipped, never fatal."""
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


def test_read_command_proofs_skips_cross_claim_and_tampered_records(
    tmp_path: Path,
) -> None:
    wrong_claim = _command_record("pytest wrong", 0, claim_id="C2")
    tampered = _command_record("pytest original", 0)
    tampered["command"] = "pytest tampered"
    _write_buffer(tmp_path, "C1", [wrong_claim, tampered, "[]"])

    assert _read_command_proofs(tmp_path, "C1") == []


def test_read_command_proofs_missing_buffer_is_empty(tmp_path: Path) -> None:
    assert _read_command_proofs(tmp_path, "NOPE") == []
