from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import io
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from anvil import signing
from anvil.claims.command_proof_artifact import (
    MAX_CLAIM_COMMAND_PROOF_BYTES,
    ClaimCommandProofError,
    load_claim_command_proof,
    load_claim_command_proof_base64,
    verify_claim_command_proof_batch,
)
from anvil.claims.progress_attestation import inspect_local_repository
from anvil.state.hashing import canonical_json_bytes, domain_separated_sha256
from anvil.state.models import (
    MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES,
    MAX_CLAIM_COMMAND_PROOF_BATCH_ITEMS,
    Claim,
    ClaimAttestationContext,
    ClaimCommandEvidenceCore,
    ClaimCommandIssuer,
    ProofKind,
    ProofRequirement,
    Task,
    TaskStatus,
    Verification,
)

_NOW = dt.datetime(2026, 8, 8, 12, 3, tzinfo=dt.UTC)
_CREATED = _NOW - dt.timedelta(minutes=3)
_STARTED = _NOW - dt.timedelta(minutes=2)
_ENDED = _NOW - dt.timedelta(minutes=1)
_COMMAND = "uv run pytest -q"
_TASK_REVISION = "a" * 64
_SEMANTIC_DOMAIN = b"anvil.command-proof.v1\0"
_CWD_DOMAIN = b"anvil.command-cwd.v1\0"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def _task(command: str = _COMMAND) -> Task:
    return Task(
        id="T008",
        feature_id="F001",
        prd_id="prd-1",
        title="proofs",
        description="d",
        status=TaskStatus.claimed,
        verification=Verification(
            required_proofs=[
                ProofRequirement(
                    kind=ProofKind.command,
                    command=command,
                    passing_exit_codes=[0],
                    label="tests",
                )
            ]
        ),
        created_at=_CREATED,
        updated_at=_CREATED,
    )


def _claim(repo: Path) -> Claim:
    local = inspect_local_repository(repo, project_id="project-1")
    return Claim(
        id="C008",
        task_id="T008",
        claimed_by="codex",
        generation=2,
        attestation_context=ClaimAttestationContext(
            repository_id=local.repository_id,
            claim_start_sha=local.head_oid,
            prd_id="prd-1",
            prd_revision=4,
            task_revision=_TASK_REVISION,
        ),
        created_at=_CREATED,
        lease_expires_at=_NOW + dt.timedelta(hours=1),
        last_heartbeat_at=_CREATED,
    )


def _core(
    repo: Path,
    *,
    command: str = _COMMAND,
    output: bytes = b"17 passed\n",
    cwd_relative: str = ".",
    started_at: dt.datetime = _STARTED,
    ended_at: dt.datetime = _ENDED,
    **updates: object,
) -> dict[str, object]:
    claim = _claim(repo)
    assert claim.attestation_context is not None
    cwd_identity = domain_separated_sha256(
        _CWD_DOMAIN,
        {
            "repository_id": claim.attestation_context.repository_id,
            "cwd_relative": cwd_relative,
        },
    )
    value: dict[str, object] = {
        "schema_version": 1,
        "project_id": "project-1",
        "claim_id": claim.id,
        "generation": claim.generation,
        "claimed_by": claim.claimed_by,
        "task_id": claim.task_id,
        "task_revision": _TASK_REVISION,
        "prd_id": "prd-1",
        "prd_revision": 4,
        "repository_id": claim.attestation_context.repository_id,
        "claim_start_sha": claim.attestation_context.claim_start_sha,
        "cwd_relative": cwd_relative,
        "cwd_identity": cwd_identity,
        "command_base64": base64.b64encode(command.encode()).decode(),
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "ended_at": ended_at.isoformat().replace("+00:00", "Z"),
        "exit_code": 0,
        "output_base64": base64.b64encode(output).decode(),
        "output_sha256": hashlib.sha256(output).hexdigest(),
    }
    value.update(updates)
    return value


def _artifact(
    repo: Path,
    *,
    envelope_id: str = "artifact-1",
    core: dict[str, object] | None = None,
    issuer: dict[str, str] | None = None,
) -> bytes:
    return canonical_json_bytes(
        {"envelope_id": envelope_id, "payload": core or _core(repo), "issuer": issuer}
    )


def _verify(repo: Path, *raw: bytes):
    return verify_claim_command_proof_batch(
        tuple(load_claim_command_proof(item) for item in raw),
        claim=_claim(repo),
        task=_task(),
        project_id="project-1",
        project_root=repo,
        actor="codex",
        declared_commands=[_COMMAND],
        now=_NOW,
    )


def test_unsigned_artifact_round_trips_exact_bytes_and_state_shape(repo: Path) -> None:
    raw = _artifact(repo)
    loaded = load_claim_command_proof(raw)
    proof = _verify(repo, raw)[0]

    assert loaded.command_bytes == _COMMAND.encode()
    assert loaded.output_bytes == b"17 passed\n"
    assert loaded.semantic_digest == domain_separated_sha256(
        _SEMANTIC_DOMAIN, loaded.evidence_core.model_dump(mode="json")
    )
    assert proof.kind == "claim_command"
    assert proof.command == _COMMAND
    assert proof.trust_mode == "claim_owner_self_attested"
    assert proof.issuer is None
    assert proof.evidence_core.output_base64 == base64.b64encode(b"17 passed\n").decode()


def test_trusted_signature_covers_the_exact_evidence_core(repo: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = signing.public_key_to_hex(private_key.public_key())
    signer_id = signing.fingerprint(public_key)
    core = _core(repo)
    issuer = {
        "algorithm": "ed25519",
        "signer_id": signer_id,
        "public_key": public_key,
        "signature": signing.sign(private_key, canonical_json_bytes(core)),
    }
    raw = _artifact(repo, core=core, issuer=issuer)

    loaded = load_claim_command_proof(raw, trusted_issuers={signer_id})
    assert loaded.trust_mode == "configured_issuer_verified"
    assert loaded.issuer_id == signer_id

    forged = dict(core, ended_at=_NOW.isoformat().replace("+00:00", "Z"))
    with pytest.raises(ClaimCommandProofError, match="signature is invalid"):
        load_claim_command_proof(
            _artifact(repo, core=forged, issuer=issuer), trusted_issuers={signer_id}
        )
    with pytest.raises(ClaimCommandProofError, match="not trusted"):
        load_claim_command_proof(raw)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"\xef\xbb\xbf{}", "bom_forbidden"),
        (b"\xff", "invalid_utf8"),
        (b'{"x":1,"x":2}', "duplicate_key"),
        (b'{"x":1.5}', "float_forbidden"),
        (b"[]", "root_not_object"),
        (b'{"envelope_id": "x"}', "noncanonical_json"),
    ],
)
def test_loader_rejects_malformed_or_noncanonical_json(raw: bytes, code: str) -> None:
    with pytest.raises(ClaimCommandProofError) as caught:
        load_claim_command_proof(raw)
    assert caught.value.code == code


def test_loader_rejects_digest_mismatch_and_nonzero_exit(repo: Path) -> None:
    bad_digest = _core(repo, output_sha256="0" * 64)
    with pytest.raises(ClaimCommandProofError) as caught:
        load_claim_command_proof(_artifact(repo, core=bad_digest))
    assert caught.value.code == "schema_invalid"

    nonzero = _core(repo, exit_code=1)
    with pytest.raises(ClaimCommandProofError) as caught:
        load_claim_command_proof(_artifact(repo, core=nonzero))
    assert caught.value.code == "schema_invalid"


class _ShortReader:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.position = 0

    def read(self, _size: int) -> bytes:
        if self.position == len(self.value):
            return b""
        chunk = self.value[self.position : self.position + 1]
        self.position += len(chunk)
        return chunk


def test_source_cap_loops_across_legal_short_reads(repo: Path) -> None:
    loaded = load_claim_command_proof(_ShortReader(_artifact(repo)))
    assert loaded.command_bytes == _COMMAND.encode()

    with pytest.raises(ClaimCommandProofError) as caught:
        load_claim_command_proof(_ShortReader(b"x" * (MAX_CLAIM_COMMAND_PROOF_BYTES + 1)))
    assert caught.value.code == "source_too_large"


def test_base64_adapter_is_strict_and_cap_checked(repo: Path) -> None:
    encoded = base64.b64encode(_artifact(repo)).decode()
    assert load_claim_command_proof_base64(encoded).command_bytes == _COMMAND.encode()
    for malformed in (encoded + "\n", encoded[:-1], "!!!!"):
        with pytest.raises(ClaimCommandProofError):
            load_claim_command_proof_base64(malformed)
    oversized = "A" * ((((MAX_CLAIM_COMMAND_PROOF_BYTES + 2) // 3) * 4) + 4)
    with pytest.raises(ClaimCommandProofError) as caught:
        load_claim_command_proof_base64(oversized)
    assert caught.value.code == "source_too_large"


def test_exact_utf8_command_and_explicit_owner_are_required(repo: Path) -> None:
    lookalike = "uv run pytest -q\u0301"
    raw = _artifact(repo, core=_core(repo, command=lookalike))
    with pytest.raises(ClaimCommandProofError) as caught:
        _verify(repo, raw)
    assert caught.value.code == "command_not_required"

    loaded = load_claim_command_proof(_artifact(repo))
    with pytest.raises(ClaimCommandProofError) as caught:
        verify_claim_command_proof_batch(
            [loaded],
            claim=_claim(repo),
            task=_task(),
            project_id="project-1",
            project_root=repo,
            actor="somebody-else",
            declared_commands=[_COMMAND],
            now=_NOW,
        )
    assert caught.value.code == "actor_mismatch"


def test_stale_binding_and_invalid_time_window_fail_closed(repo: Path) -> None:
    stale = _core(repo, generation=1)
    with pytest.raises(ClaimCommandProofError) as caught:
        _verify(repo, _artifact(repo, core=stale))
    assert caught.value.code == "claim_binding_mismatch"

    before_claim = _core(repo, started_at=_CREATED - dt.timedelta(seconds=1))
    with pytest.raises(ClaimCommandProofError) as caught:
        _verify(repo, _artifact(repo, core=before_claim))
    assert caught.value.code == "time_window_invalid"

    alias = _core(repo)
    alias["ended_at"] = _ENDED.isoformat()
    with pytest.raises(ClaimCommandProofError) as caught:
        _verify(repo, _artifact(repo, core=alias))
    assert caught.value.code == "schema_invalid"


def test_cwd_identity_is_canonical_contained_and_directory_bound(repo: Path) -> None:
    (repo / "subdir").mkdir()
    raw = _artifact(repo, core=_core(repo, cwd_relative="subdir"))
    assert _verify(repo, raw)[0].evidence_core.cwd_relative == "subdir"

    traversal = _core(repo, cwd_relative="../outside")
    with pytest.raises(ClaimCommandProofError) as caught:
        _verify(repo, _artifact(repo, core=traversal))
    assert caught.value.code == "path_traversal"

    wrong_identity = _core(repo, cwd_relative="subdir", cwd_identity="0" * 64)
    with pytest.raises(ClaimCommandProofError) as caught:
        _verify(repo, _artifact(repo, core=wrong_identity))
    assert caught.value.code == "cwd_mismatch"


def test_cwd_symlink_or_reparse_point_is_refused(repo: Path) -> None:
    (repo / "real").mkdir()
    try:
        (repo / "linked").symlink_to(repo / "real", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    with pytest.raises(ClaimCommandProofError) as caught:
        _verify(repo, _artifact(repo, core=_core(repo, cwd_relative="linked")))
    assert caught.value.code == "cwd_link_forbidden"


def test_batch_rejects_duplicate_partial_and_resource_overruns_atomically(repo: Path) -> None:
    first = load_claim_command_proof(_artifact(repo, envelope_id="first"))
    same_evidence = load_claim_command_proof(_artifact(repo, envelope_id="second"))
    with pytest.raises(ClaimCommandProofError) as caught:
        verify_claim_command_proof_batch(
            [first, same_evidence],
            claim=_claim(repo),
            task=_task(),
            project_id="project-1",
            project_root=repo,
            actor="codex",
            declared_commands=[_COMMAND],
            now=_NOW,
        )
    assert caught.value.code == "duplicate_evidence"

    with pytest.raises(ClaimCommandProofError) as caught:
        verify_claim_command_proof_batch(
            [first] * (MAX_CLAIM_COMMAND_PROOF_BATCH_ITEMS + 1),
            claim=_claim(repo),
            task=_task(),
            project_id="project-1",
            project_root=repo,
            actor="codex",
            declared_commands=[_COMMAND],
            now=_NOW,
        )
    assert caught.value.code == "batch_size"

    inflated = dataclasses.replace(first, raw_size_bytes=MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES)
    with pytest.raises(ClaimCommandProofError) as caught:
        verify_claim_command_proof_batch(
            [inflated, first],
            claim=_claim(repo),
            task=_task(),
            project_id="project-1",
            project_root=repo,
            actor="codex",
            declared_commands=[_COMMAND],
            now=_NOW,
        )
    assert caught.value.code == "batch_too_large"


def test_unknown_fields_and_command_or_output_byte_caps_are_rejected(repo: Path) -> None:
    unknown = _core(repo, invented=True)
    with pytest.raises(ClaimCommandProofError) as caught:
        load_claim_command_proof(_artifact(repo, core=unknown))
    assert caught.value.code == "schema_invalid"

    too_much_output = b"x" * 131_073
    oversized = _core(repo, output=too_much_output)
    with pytest.raises(ClaimCommandProofError) as caught:
        load_claim_command_proof(_artifact(repo, core=oversized))
    assert caught.value.code == "schema_invalid"


def test_binary_output_is_opaque_and_rehashed(repo: Path) -> None:
    output = b"\xff\x00\r\n\x80"
    proof = _verify(repo, _artifact(repo, core=_core(repo, output=output)))[0]
    assert base64.b64decode(proof.evidence_core.output_base64) == output
    assert proof.output_sha256 == hashlib.sha256(output).hexdigest()


def test_claim_context_and_active_lease_are_mandatory(repo: Path) -> None:
    loaded = load_claim_command_proof(_artifact(repo))
    claim = _claim(repo)
    claim.attestation_context = None
    with pytest.raises(ClaimCommandProofError) as caught:
        verify_claim_command_proof_batch(
            [loaded],
            claim=claim,
            task=_task(),
            project_id="project-1",
            project_root=repo,
            actor="codex",
            declared_commands=[_COMMAND],
            now=_NOW,
        )
    assert caught.value.code == "claim_context_missing"

    claim = _claim(repo)
    claim.status = "released"
    with pytest.raises(ClaimCommandProofError) as caught:
        verify_claim_command_proof_batch(
            [loaded],
            claim=claim,
            task=_task(),
            project_id="project-1",
            project_root=repo,
            actor="codex",
            declared_commands=[_COMMAND],
            now=_NOW,
        )
    assert caught.value.code == "claim_inactive"


def test_stream_must_be_binary() -> None:
    with pytest.raises(ClaimCommandProofError) as caught:
        load_claim_command_proof(io.StringIO("{}"))
    assert caught.value.code == "source_invalid"


def test_core_model_rejects_noncanonical_base64_directly(repo: Path) -> None:
    core = _core(repo)
    core["command_base64"] = str(core["command_base64"]) + "="
    with pytest.raises(ValidationError):
        ClaimCommandEvidenceCore.model_validate(core)

    with pytest.raises(ValidationError):
        ClaimCommandIssuer(
            algorithm="ed25519",
            signer_id="0" * 16,
            public_key="0" * 64,
            signature="0" * 127,
        )
