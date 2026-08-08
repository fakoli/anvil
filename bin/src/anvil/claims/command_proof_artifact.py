"""Strict, claim-bound command-proof artifact loading and verification.

The artifact is intentionally passive: it proves bytes reported by a claim owner or
configured issuer, but never executes a command.  All hostile bytes are bounded and
canonicalized before they can enter durable state.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import os
import re
import stat
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from anvil import signing
from anvil.claims.progress_attestation import (
    ProgressAttestationError,
    canonical_progress_path,
    inspect_local_repository,
)
from anvil.state.hashing import (
    CanonicalJsonRefusal,
    canonical_json_bytes,
    canonical_node_budget_for_bytes,
    domain_separated_sha256,
)
from anvil.state.models import (
    MAX_CLAIM_COMMAND_PROOF_ARTIFACT_BYTES,
    MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES,
    MAX_CLAIM_COMMAND_PROOF_BATCH_ITEMS,
    Claim,
    ClaimCommandEvidenceCore,
    ClaimCommandIssuer,
    ClaimCommandProof,
    ClaimStatus,
    ProofKind,
    Task,
    claim_command_semantic_projection,
)

MAX_CLAIM_COMMAND_PROOF_BYTES = MAX_CLAIM_COMMAND_PROOF_ARTIFACT_BYTES

_SEMANTIC_DOMAIN = b"anvil.command-proof.v1\0"
_CWD_DOMAIN = b"anvil.command-cwd.v1\0"
_UTF8_BOM = b"\xef\xbb\xbf"


class ClaimCommandProofError(ValueError):
    """Stable, value-safe refusal from the command-proof boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _ClaimCommandEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    envelope_id: StrictStr = Field(min_length=1, max_length=255)
    payload: ClaimCommandEvidenceCore
    issuer: ClaimCommandIssuer | None = None


@dataclass(frozen=True, slots=True)
class LoadedClaimCommandProof:
    """A canonical, authenticated envelope awaiting live claim verification."""

    envelope_id: str
    evidence_core: ClaimCommandEvidenceCore
    semantic_digest: str
    semantic_bytes: bytes
    command_bytes: bytes
    output_bytes: bytes
    trust_mode: Literal["claim_owner_self_attested", "configured_issuer_verified"]
    issuer_id: str | None
    issuer: ClaimCommandIssuer | None
    raw_size_bytes: int


def load_claim_command_proof(
    source: bytes | BinaryIO,
    *,
    trusted_issuers: Collection[str] = frozenset(),
) -> LoadedClaimCommandProof:
    """Load one bounded canonical JSON envelope and authenticate its issuer."""
    raw = _bounded_source_bytes(source)
    if raw.startswith(_UTF8_BOM):
        raise ClaimCommandProofError("bom_forbidden", "command proof must not contain a BOM")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ClaimCommandProofError("invalid_utf8", "command proof must be valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except ClaimCommandProofError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ClaimCommandProofError("invalid_json", "command proof is not valid JSON") from exc
    if type(value) is not dict:
        raise ClaimCommandProofError("root_not_object", "command proof root must be an object")
    try:
        canonical = canonical_json_bytes(
            value,
            max_bytes=MAX_CLAIM_COMMAND_PROOF_BYTES,
            max_nodes=canonical_node_budget_for_bytes(MAX_CLAIM_COMMAND_PROOF_BYTES),
            max_string_bytes=MAX_CLAIM_COMMAND_PROOF_BYTES,
        )
    except (CanonicalJsonRefusal, ValueError) as exc:
        raise ClaimCommandProofError(
            "noncanonical_json", "command proof JSON is not canonical"
        ) from exc
    if canonical != raw:
        raise ClaimCommandProofError(
            "noncanonical_json", "command proof bytes must exactly match canonical JSON"
        )
    try:
        envelope = _ClaimCommandEnvelope.model_validate(value)
    except ValidationError as exc:
        raise ClaimCommandProofError("schema_invalid", "command proof schema is invalid") from exc

    core_value = envelope.payload.model_dump(mode="json")
    semantic_bytes = canonical_json_bytes(
        core_value,
        max_bytes=MAX_CLAIM_COMMAND_PROOF_BYTES,
        max_nodes=canonical_node_budget_for_bytes(MAX_CLAIM_COMMAND_PROOF_BYTES),
        max_string_bytes=MAX_CLAIM_COMMAND_PROOF_BYTES,
    )
    semantic_digest = domain_separated_sha256(
        _SEMANTIC_DOMAIN,
        claim_command_semantic_projection(envelope.payload),
        max_bytes=MAX_CLAIM_COMMAND_PROOF_BYTES,
        max_nodes=canonical_node_budget_for_bytes(MAX_CLAIM_COMMAND_PROOF_BYTES),
        max_string_bytes=MAX_CLAIM_COMMAND_PROOF_BYTES,
    )
    issuer_id: str | None = None
    trust_mode: Literal["claim_owner_self_attested", "configured_issuer_verified"]
    if envelope.issuer is None:
        trust_mode = "claim_owner_self_attested"
    else:
        issuer = envelope.issuer
        try:
            genuine_id = signing.fingerprint(issuer.public_key)
            trusted = signing.is_trusted(issuer.public_key, set(trusted_issuers))
        except ValueError as exc:
            raise ClaimCommandProofError(
                "issuer_invalid", "command proof issuer is invalid"
            ) from exc
        if genuine_id != issuer.signer_id:
            raise ClaimCommandProofError(
                "issuer_mismatch", "command proof issuer identity mismatches"
            )
        if not trusted:
            raise ClaimCommandProofError("issuer_untrusted", "command proof issuer is not trusted")
        if not signing.verify(issuer.public_key, semantic_bytes, issuer.signature):
            raise ClaimCommandProofError("signature_invalid", "command proof signature is invalid")
        trust_mode = "configured_issuer_verified"
        issuer_id = genuine_id

    return LoadedClaimCommandProof(
        envelope_id=envelope.envelope_id,
        evidence_core=envelope.payload,
        semantic_digest=semantic_digest,
        semantic_bytes=semantic_bytes,
        command_bytes=base64.b64decode(envelope.payload.command_base64, validate=True),
        output_bytes=base64.b64decode(envelope.payload.output_base64, validate=True),
        trust_mode=trust_mode,
        issuer_id=issuer_id,
        issuer=envelope.issuer,
        raw_size_bytes=len(raw),
    )


def load_claim_command_proof_base64(
    value: str,
    *,
    trusted_issuers: Collection[str] = frozenset(),
) -> LoadedClaimCommandProof:
    """Strict base64 adapter that enforces the decoded cap before allocation."""
    if type(value) is not str or not value or any(char.isspace() for char in value):
        raise ClaimCommandProofError("base64_invalid", "command proof base64 is invalid")
    max_encoded = ((MAX_CLAIM_COMMAND_PROOF_BYTES + 2) // 3) * 4
    if len(value) > max_encoded:
        raise ClaimCommandProofError("source_too_large", "command proof exceeds the byte limit")
    if len(value) % 4:
        raise ClaimCommandProofError("base64_invalid", "command proof base64 is invalid")
    padding = 2 if value.endswith("==") else 1 if value.endswith("=") else 0
    if (len(value) // 4) * 3 - padding > MAX_CLAIM_COMMAND_PROOF_BYTES:
        raise ClaimCommandProofError("source_too_large", "command proof exceeds the byte limit")
    try:
        encoded = value.encode("ascii", errors="strict")
        raw = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ClaimCommandProofError("base64_invalid", "command proof base64 is invalid") from exc
    if base64.b64encode(raw) != encoded:
        raise ClaimCommandProofError("base64_noncanonical", "command proof base64 is not canonical")
    return load_claim_command_proof(raw, trusted_issuers=trusted_issuers)


def verify_claim_command_proof_batch(
    loaded_proofs: Sequence[LoadedClaimCommandProof],
    *,
    claim: Claim,
    task: Task,
    project_id: str,
    project_root: Path,
    actor: str,
    declared_commands: Collection[str],
    now: dt.datetime,
) -> tuple[ClaimCommandProof, ...]:
    """Prevalidate a whole batch against one explicit live claim, then return it."""
    if isinstance(loaded_proofs, (str, bytes)) or not isinstance(loaded_proofs, Sequence):
        raise ClaimCommandProofError("batch_invalid", "command proof batch is invalid")
    if not 1 <= len(loaded_proofs) <= MAX_CLAIM_COMMAND_PROOF_BATCH_ITEMS:
        raise ClaimCommandProofError("batch_size", "command proof batch is outside limits")
    if any(not isinstance(item, LoadedClaimCommandProof) for item in loaded_proofs):
        raise ClaimCommandProofError("batch_invalid", "command proof batch is invalid")
    if sum(item.raw_size_bytes for item in loaded_proofs) > MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES:
        raise ClaimCommandProofError(
            "batch_too_large", "command proof batch exceeds its byte limit"
        )

    verified_at = _aware_utc(now, "now")
    if type(actor) is not str or not actor.strip() or actor != claim.claimed_by:
        raise ClaimCommandProofError("actor_mismatch", "command proof actor mismatches claim owner")
    if claim.status != ClaimStatus.active or claim.released_at is not None:
        raise ClaimCommandProofError("claim_inactive", "command proof claim is not active")
    if verified_at >= _aware_utc(claim.lease_expires_at, "lease_expires_at"):
        raise ClaimCommandProofError("claim_expired", "command proof claim lease has expired")
    if claim.attestation_context is None:
        raise ClaimCommandProofError(
            "claim_context_missing", "command proof requires immutable claim context"
        )
    context = claim.attestation_context
    if task.id != claim.task_id or task.prd_id != context.prd_id:
        raise ClaimCommandProofError("task_mismatch", "command proof task mismatches claim")
    try:
        project_id_size = len(project_id.encode("utf-8", errors="strict"))
    except (AttributeError, UnicodeEncodeError) as exc:
        raise ClaimCommandProofError("project_invalid", "command proof project is invalid") from exc
    if type(project_id) is not str or not project_id or project_id_size > 255:
        raise ClaimCommandProofError("project_invalid", "command proof project is invalid")
    local = _inspect_repository(project_root, project_id=project_id)
    if local.repository_id != context.repository_id:
        raise ClaimCommandProofError(
            "repository_mismatch", "local repository identity mismatches claim context"
        )

    try:
        required_command_bytes = {
            requirement.command.encode("utf-8", errors="strict")
            for requirement in task.verification.required_proofs
            if requirement.kind == ProofKind.command
            and requirement.command is not None
            and 0 in requirement.passing_exit_codes
        }
    except UnicodeEncodeError as exc:
        raise ClaimCommandProofError(
            "command_requirement_invalid", "task command requirement is invalid"
        ) from exc
    if isinstance(declared_commands, (str, bytes)):
        raise ClaimCommandProofError("command_declaration_invalid", "declared commands are invalid")
    try:
        if any(type(command) is not str for command in declared_commands):
            raise TypeError
        declared_command_bytes = {
            command.encode("utf-8", errors="strict") for command in declared_commands
        }
    except (TypeError, UnicodeEncodeError) as exc:
        raise ClaimCommandProofError(
            "command_declaration_invalid", "declared commands are invalid"
        ) from exc
    if not required_command_bytes:
        raise ClaimCommandProofError(
            "command_not_required", "task has no passing command proof requirement"
        )

    created_at = _aware_utc(claim.created_at, "claim.created_at")
    lease_expires_at = _aware_utc(claim.lease_expires_at, "claim.lease_expires_at")
    digests: set[str] = set()
    verified: list[ClaimCommandProof] = []
    for loaded in loaded_proofs:
        core = loaded.evidence_core
        expected = (
            (core.project_id, project_id),
            (core.claim_id, claim.id),
            (core.generation, claim.generation),
            (core.claimed_by, actor),
            (core.task_id, task.id),
            (core.task_revision, context.task_revision),
            (core.prd_id, context.prd_id),
            (core.prd_revision, context.prd_revision),
            (core.repository_id, context.repository_id),
            (core.claim_start_sha, context.claim_start_sha),
        )
        if any(actual != bound for actual, bound in expected):
            raise ClaimCommandProofError(
                "claim_binding_mismatch", "command proof claim binding mismatches"
            )
        if loaded.semantic_digest in digests:
            raise ClaimCommandProofError(
                "duplicate_evidence", "command proof batch repeats identical evidence"
            )
        digests.add(loaded.semantic_digest)
        if loaded.command_bytes not in required_command_bytes:
            raise ClaimCommandProofError(
                "command_not_required", "command proof does not match a task requirement"
            )
        if loaded.command_bytes not in declared_command_bytes:
            raise ClaimCommandProofError(
                "command_not_declared", "command proof does not match submitted commands"
            )
        cwd_relative, cwd, cwd_identity = _claim_command_cwd_binding(
            local.root,
            repository_id=local.repository_id,
            cwd_relative=core.cwd_relative,
        )
        if core.cwd_identity != cwd_identity or not cwd.is_dir():
            raise ClaimCommandProofError(
                "cwd_mismatch", "command proof working directory identity mismatches"
            )
        started_at = _parse_time(core.started_at, "started_at")
        ended_at = _parse_time(core.ended_at, "ended_at")
        if not created_at <= started_at <= ended_at <= verified_at:
            raise ClaimCommandProofError(
                "time_window_invalid", "command proof time window is invalid"
            )
        if ended_at > lease_expires_at:
            raise ClaimCommandProofError(
                "time_window_invalid", "command proof ended after the claim lease"
            )
        try:
            command = loaded.command_bytes.decode("utf-8", errors="strict")
            proof = ClaimCommandProof(
                command=command,
                exit_code=core.exit_code,
                output_sha256=core.output_sha256,
                captured_at=ended_at,
                semantic_digest=loaded.semantic_digest,
                trust_mode=loaded.trust_mode,
                issuer_id=loaded.issuer_id,
                evidence_core=core,
                issuer=loaded.issuer,
            )
        except (UnicodeDecodeError, ValidationError) as exc:  # schema already checked
            raise ClaimCommandProofError(
                "proof_invalid", "verified command proof cannot be represented"
            ) from exc
        verified.append(proof)
    try:
        durable_bytes = sum(
            len(
                canonical_json_bytes(
                    proof.model_dump(mode="json"),
                    max_bytes=MAX_CLAIM_COMMAND_PROOF_ARTIFACT_BYTES,
                    max_nodes=canonical_node_budget_for_bytes(
                        MAX_CLAIM_COMMAND_PROOF_ARTIFACT_BYTES
                    ),
                    max_string_bytes=MAX_CLAIM_COMMAND_PROOF_ARTIFACT_BYTES,
                )
            )
            for proof in verified
        )
    except (CanonicalJsonRefusal, ValueError) as exc:
        raise ClaimCommandProofError(
            "proof_too_large", "persisted command proof exceeds its byte limit"
        ) from exc
    if durable_bytes > MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES:
        raise ClaimCommandProofError(
            "batch_too_large", "persisted command proof batch exceeds its byte limit"
        )
    return tuple(verified)


def _bounded_source_bytes(source: bytes | BinaryIO) -> bytes:
    if isinstance(source, bytes):
        raw = source
    elif hasattr(source, "read"):
        buffered = bytearray()
        try:
            while len(buffered) <= MAX_CLAIM_COMMAND_PROOF_BYTES:
                chunk = source.read(MAX_CLAIM_COMMAND_PROOF_BYTES + 1 - len(buffered))
                if not isinstance(chunk, bytes):
                    raise ClaimCommandProofError(
                        "source_invalid", "command proof stream must be binary"
                    )
                if not chunk:
                    break
                buffered.extend(chunk)
        except ClaimCommandProofError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise ClaimCommandProofError("source_unavailable", "cannot read command proof") from exc
        raw = bytes(buffered)
    else:
        raise ClaimCommandProofError("source_invalid", "command proof source must be bytes")
    if len(raw) > MAX_CLAIM_COMMAND_PROOF_BYTES:
        raise ClaimCommandProofError("source_too_large", "command proof exceeds the byte limit")
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ClaimCommandProofError("duplicate_key", "command proof contains duplicate key")
        result[key] = value
    return result


def _reject_float(_value: str) -> Any:
    raise ClaimCommandProofError("float_forbidden", "command proof cannot contain floats")


def _reject_constant(_value: str) -> Any:
    raise ClaimCommandProofError(
        "constant_forbidden", "command proof cannot contain non-finite values"
    )


def _aware_utc(value: dt.datetime, name: str) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ClaimCommandProofError("time_invalid", f"{name} must be timezone-aware")
    return value.astimezone(dt.UTC)


def _parse_time(value: str, name: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ClaimCommandProofError("time_invalid", f"{name} is invalid") from exc
    canonical = _aware_utc(parsed, name)
    if canonical.isoformat().replace("+00:00", "Z") != value:
        raise ClaimCommandProofError("time_noncanonical", f"{name} must use canonical UTC ISO 8601")
    return canonical


def _inspect_repository(project_root: Path, *, project_id: str) -> Any:
    try:
        return inspect_local_repository(project_root, project_id=project_id)
    except ProgressAttestationError as exc:
        raise ClaimCommandProofError(exc.code, str(exc)) from exc


def claim_command_cwd_identity(
    project_root: Path,
    repository_id: str,
    cwd_relative: str,
) -> str:
    """Bind one canonical contained directory to its stable filesystem identity."""
    return _claim_command_cwd_binding(
        project_root,
        repository_id=repository_id,
        cwd_relative=cwd_relative,
    )[2]


def _claim_command_cwd_binding(
    project_root: Path,
    *,
    repository_id: str,
    cwd_relative: str,
) -> tuple[str, Path, str]:
    if type(repository_id) is not str or not re.fullmatch(r"[0-9a-f]{64}", repository_id):
        raise ClaimCommandProofError(
            "repository_invalid", "command proof repository identity is invalid"
        )
    relative, resolved, device, inode = _verified_cwd(project_root, cwd_relative)
    identity = domain_separated_sha256(
        _CWD_DOMAIN,
        {
            "repository_id": repository_id,
            "filesystem_device": device,
            "filesystem_inode": inode,
        },
        max_bytes=16_384,
        max_string_bytes=8_192,
    )
    return relative, resolved, identity


def _verified_cwd(root: Path, value: str) -> tuple[str, Path, int, int]:
    if value == ".":
        relative = "."
        parts: tuple[str, ...] = ()
    else:
        try:
            relative = canonical_progress_path(value)
        except ProgressAttestationError as exc:
            raise ClaimCommandProofError(exc.code, str(exc)) from exc
        if relative != value:
            raise ClaimCommandProofError("cwd_noncanonical", "command proof cwd is not canonical")
        parts = tuple(relative.split("/"))
    current = root
    try:
        root_resolved = root.resolve(strict=True)
        final_info = current.lstat()
        if stat.S_ISLNK(final_info.st_mode) or (
            os.name == "nt" and final_info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ClaimCommandProofError(
                "cwd_link_forbidden", "command proof cwd cannot traverse a link"
            )
        for part in parts:
            current = current / part
            final_info = current.lstat()
            if stat.S_ISLNK(final_info.st_mode) or (
                os.name == "nt"
                and final_info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ClaimCommandProofError(
                    "cwd_link_forbidden", "command proof cwd cannot traverse a link"
                )
            if not stat.S_ISDIR(final_info.st_mode):
                raise ClaimCommandProofError("cwd_invalid", "command proof cwd is not a directory")
        resolved = current.resolve(strict=True)
        resolved.relative_to(root_resolved)
        stable_info = current.lstat()
        if (
            final_info.st_dev != stable_info.st_dev
            or final_info.st_ino != stable_info.st_ino
            or final_info.st_mode != stable_info.st_mode
            or not stat.S_ISDIR(stable_info.st_mode)
            or stable_info.st_dev < 0
            or stable_info.st_ino <= 0
        ):
            raise ClaimCommandProofError(
                "cwd_identity_unavailable",
                "command proof cwd has no stable filesystem identity",
            )
    except ClaimCommandProofError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ClaimCommandProofError(
            "cwd_unavailable", "command proof cwd is unavailable or outside the repository"
        ) from exc
    return relative, resolved, int(stable_info.st_dev), int(stable_info.st_ino)
