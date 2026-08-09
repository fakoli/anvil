"""Fail-closed verification for claim-bound external progress attestations.

The module is deliberately below the CLI, MCP, and state-manager surfaces.  It
owns the hostile-input boundary, local repository/file checks, and the stable
plain dictionaries those callers persist.  No verifier decision depends on an
untrusted display string or on shell parsing.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime as dt
import errno
import hashlib
import json
import ntpath
import os
import re
import stat
import subprocess
import threading
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from anvil import signing
from anvil.state.hashing import (
    CanonicalJsonRefusal,
    canonical_json_bytes,
    canonical_node_budget_for_bytes,
    domain_separated_sha256,
)

MAX_PROGRESS_ATTESTATION_BYTES = 262_144
MAX_PROGRESS_PATHS = 256
MAX_PROGRESS_PATH_UTF8_BYTES = 4_096
MAX_GIT_OUTPUT_BYTES = 65_536
GIT_TIMEOUT_SECONDS = 10.0

_SEMANTIC_DOMAIN = b"anvil.progress-attestation.v1\0"
_REPOSITORY_DOMAIN = b"anvil.progress-repository.v1\0"
_TASK_DOMAIN = b"anvil.progress-task.v1\0"
_WINDOWS_RESERVED = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9\u00b9\u00b2\u00b3]|"
    r"LPT[1-9\u00b9\u00b2\u00b3])(?:\..*)?$",
    re.IGNORECASE,
)


class ProgressAttestationError(ValueError):
    """Typed, value-safe refusal at the progress-attestation boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _AttestationPayloadBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    kind: StrictStr
    project_id: StrictStr = Field(min_length=1, max_length=255)
    claim_id: StrictStr = Field(min_length=1, max_length=255)
    generation: StrictInt = Field(ge=1)
    task_id: StrictStr = Field(min_length=1, max_length=255)
    task_revision: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    prd_id: StrictStr = Field(min_length=1, max_length=255)
    prd_revision: StrictInt = Field(ge=1)
    claimed_by: StrictStr = Field(min_length=1, max_length=4096)
    repository_id: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    claim_start_sha: StrictStr = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    commit_sha: StrictStr = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    path: StrictStr = Field(min_length=1, max_length=4096)
    prior_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    file_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: dt.datetime

    @field_validator("schema_version", mode="before")
    @classmethod
    def _strict_schema_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @field_validator("issued_at", mode="before")
    @classmethod
    def _utc_issued_at(cls, value: Any) -> dt.datetime:
        if type(value) is not str:
            raise ValueError("issued_at must be an ISO 8601 string")
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("issued_at must be an ISO 8601 string") from None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("issued_at must be timezone-aware")
        return value.astimezone(dt.UTC)

    @field_validator("claimed_by")
    @classmethod
    def _nonblank_actor(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("claimed_by must not be blank")
        return value


class CommitProgressPayload(_AttestationPayloadBase):
    """Version 1 attestation for one expected path in a Git commit."""

    kind: Literal["commit"]


class FileProgressPayload(_AttestationPayloadBase):
    """Version 1 attestation for one expected working-tree file."""

    kind: Literal["file"]


ProgressPayload: TypeAlias = Annotated[
    CommitProgressPayload | FileProgressPayload,
    Field(discriminator="kind"),
]


class ProgressIssuer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: Literal["ed25519"]
    signer_id: StrictStr = Field(min_length=1, max_length=255)
    public_key: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    signature: StrictStr = Field(pattern=r"^[0-9a-f]{128}$")


class ProgressEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    envelope_id: StrictStr = Field(min_length=1, max_length=255)
    payload: ProgressPayload
    issuer: ProgressIssuer | None = None


@dataclass(frozen=True, slots=True)
class PathBaseline:
    path: str
    baseline_sha256: str | None

    def model_dump(self) -> dict[str, str | None]:
        return {"path": self.path, "baseline_sha256": self.baseline_sha256}


class _StoredPathBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: StrictStr = Field(min_length=1, max_length=4096)
    baseline_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class _StoredClaimContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_id: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    claim_start_sha: StrictStr = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    prd_id: StrictStr = Field(min_length=1, max_length=255)
    prd_revision: StrictInt = Field(ge=1)
    task_revision: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    expected_paths: list[_StoredPathBaseline] = Field(
        max_length=MAX_PROGRESS_PATHS,
    )

    @model_validator(mode="after")
    def _canonical_unique_paths(self) -> _StoredClaimContext:
        paths = [entry.path for entry in self.expected_paths]
        if any(canonical_progress_path(path) != path for path in paths):
            raise ValueError("stored progress paths must be canonical")
        normalized = [os.path.normcase(path) for path in paths]
        if len(normalized) != len(set(normalized)):
            raise ValueError("stored progress paths must be unique")
        return self


@dataclass(frozen=True, slots=True)
class ClaimProgressContext:
    """Runtime claim binding plus its verbatim durable context representation."""

    project_id: str
    claim_id: str
    generation: int
    task_id: str
    claimed_by: str
    claim_created_at: dt.datetime
    repository_id: str
    claim_start_sha: str
    object_format: Literal["sha1", "sha256"]
    prd_id: str
    prd_revision: int
    task_revision: str
    expected_paths: tuple[PathBaseline, ...]

    def context_dict(self) -> dict[str, Any]:
        """Return exactly the JSON shape stored in ``Claim.attestation_context``."""
        return {
            "repository_id": self.repository_id,
            "claim_start_sha": self.claim_start_sha,
            "prd_id": self.prd_id,
            "prd_revision": self.prd_revision,
            "task_revision": self.task_revision,
            "expected_paths": [entry.model_dump() for entry in self.expected_paths],
        }

    def model_dump(self) -> dict[str, Any]:
        """Compatibility alias for state-layer callers using Pydantic vocabulary."""
        return self.context_dict()

    @classmethod
    def from_context_dict(
        cls,
        value: Mapping[str, Any],
        *,
        project_id: str,
        claim_id: str,
        generation: int,
        task_id: str,
        claimed_by: str,
        claim_created_at: dt.datetime,
        object_format: Literal["sha1", "sha256"] | None = None,
    ) -> ClaimProgressContext:
        """Rebind a replayed durable context to its authoritative Claim fields."""
        try:
            stored = _StoredClaimContext.model_validate(value)
        except ValidationError as exc:
            raise ProgressAttestationError(
                "context_invalid", "claim attestation context is invalid"
            ) from exc
        fmt = object_format or ("sha1" if len(stored.claim_start_sha) == 40 else "sha256")
        if fmt not in {"sha1", "sha256"}:
            raise ProgressAttestationError("context_invalid", "Git object format is invalid")
        _validate_oid(stored.claim_start_sha, fmt)
        created = _require_aware_utc(claim_created_at, "claim_created_at")
        return cls(
            project_id=_bounded_identity(project_id, "project_id"),
            claim_id=_bounded_identity(claim_id, "claim_id"),
            generation=_positive_int(generation, "generation"),
            task_id=_bounded_identity(task_id, "task_id"),
            claimed_by=_bounded_identity(claimed_by, "claimed_by", max_bytes=4096),
            claim_created_at=created,
            repository_id=stored.repository_id,
            claim_start_sha=stored.claim_start_sha,
            object_format=fmt,
            prd_id=stored.prd_id,
            prd_revision=stored.prd_revision,
            task_revision=stored.task_revision,
            expected_paths=tuple(
                PathBaseline(path=item.path, baseline_sha256=item.baseline_sha256)
                for item in stored.expected_paths
            ),
        )


@dataclass(frozen=True, slots=True)
class LoadedProgressAttestation:
    envelope_id: str
    payload: CommitProgressPayload | FileProgressPayload
    semantic_digest: str
    semantic_bytes: bytes
    evidence_core: Mapping[str, Any]
    signed_payload: Mapping[str, Any]
    issuer: Mapping[str, Any] | None
    trust_mode: Literal["claim_owner_self_attested", "configured_issuer_verified"]
    issuer_id: str | None
    raw_size_bytes: int


@dataclass(frozen=True, slots=True)
class VerifiedProgressAttestation:
    """Verified evidence plus the exact state-event payload to persist."""

    loaded: LoadedProgressAttestation
    state_payload: Mapping[str, Any]

    def model_dump(self) -> dict[str, Any]:
        return dict(self.state_payload)


def canonical_progress_path(value: str) -> str:
    """Return one portable project-relative path or refuse unsafe spellings."""
    if type(value) is not str:
        raise ProgressAttestationError("path_invalid", "progress path must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProgressAttestationError("path_invalid_unicode", "progress path is invalid") from exc
    if not encoded or len(encoded) > MAX_PROGRESS_PATH_UTF8_BYTES:
        raise ProgressAttestationError("path_invalid", "progress path is outside size limits")
    if any(ord(char) <= 31 or 127 <= ord(char) <= 159 for char in value):
        raise ProgressAttestationError("path_control", "progress path contains a control character")
    if value.startswith(("/", "\\")) or ntpath.splitdrive(value)[0]:
        raise ProgressAttestationError("path_absolute", "progress path must be project-relative")
    portable = value.replace("\\", "/")
    if portable.startswith("//") or any(char in portable for char in '<>"|?*'):
        raise ProgressAttestationError("path_invalid", "progress path uses a forbidden spelling")
    parts = portable.split("/")
    if any(part == "" for part in parts):
        raise ProgressAttestationError("path_empty_segment", "progress path has an empty segment")
    canonical: list[str] = []
    for part in parts:
        if part == ".":
            continue
        if part == "..":
            raise ProgressAttestationError("path_traversal", "progress path cannot traverse upward")
        if ":" in part:
            raise ProgressAttestationError(
                "path_ads", "progress path cannot name an alternate stream"
            )
        if part.endswith((".", " ")):
            raise ProgressAttestationError(
                "path_trailing_dot_space", "progress path has an unsafe trailing character"
            )
        if _WINDOWS_RESERVED.fullmatch(part):
            raise ProgressAttestationError(
                "path_reserved", "progress path contains a reserved device name"
            )
        canonical.append(part)
    if not canonical:
        raise ProgressAttestationError("path_invalid", "progress path must name a file")
    return "/".join(canonical)


def capture_claim_progress_context(
    project_root: Path,
    *,
    project_id: str,
    claim_id: str,
    claim_generation: int,
    task_id: str,
    task_snapshot: Any,
    prd_id: str,
    prd_revision: int,
    actor: str,
    claim_created_at: dt.datetime,
    expected_paths: Sequence[str],
    claim_start_sha: str | None = None,
) -> ClaimProgressContext:
    """Capture immutable claim-start Git, task, and expected-path identities."""
    local = _local_repository(project_root, project_id=project_id)
    planned_start = claim_start_sha or local.head_oid
    _validate_oid(planned_start, local.object_format)
    resolved_start = _git_text(
        local.root, "rev-parse", "--verify", f"{planned_start}^{{commit}}"
    )
    if resolved_start != planned_start:
        raise ProgressAttestationError(
            "claim_start_invalid", "planned claim start does not resolve exactly"
        )
    paths = _canonical_expected_paths(expected_paths)
    baselines = tuple(
        PathBaseline(
            path=path,
            baseline_sha256=_capture_expected_baseline(
                local,
                path,
                claim_start_sha=planned_start,
                check_worktree=claim_start_sha is None,
            ),
        )
        for path in paths
    )
    snapshot = _json_snapshot(task_snapshot)
    try:
        task_revision = domain_separated_sha256(
            _TASK_DOMAIN,
            snapshot,
            max_bytes=MAX_PROGRESS_ATTESTATION_BYTES,
            max_nodes=canonical_node_budget_for_bytes(MAX_PROGRESS_ATTESTATION_BYTES),
            max_string_bytes=MAX_PROGRESS_ATTESTATION_BYTES,
        )
    except (CanonicalJsonRefusal, ValueError) as exc:
        raise ProgressAttestationError(
            "task_snapshot_invalid", "task snapshot cannot be canonically hashed"
        ) from exc
    return ClaimProgressContext(
        project_id=_bounded_identity(project_id, "project_id"),
        claim_id=_bounded_identity(claim_id, "claim_id"),
        generation=_positive_int(claim_generation, "claim_generation"),
        task_id=_bounded_identity(task_id, "task_id"),
        claimed_by=_bounded_identity(actor, "actor", max_bytes=4096),
        claim_created_at=_require_aware_utc(claim_created_at, "claim_created_at"),
        repository_id=local.repository_id,
        claim_start_sha=planned_start,
        object_format=local.object_format,
        prd_id=_bounded_identity(prd_id, "prd_id"),
        prd_revision=_positive_int(prd_revision, "prd_revision"),
        task_revision=task_revision,
        expected_paths=baselines,
    )


def load_progress_attestation(
    source: bytes | BinaryIO,
    *,
    trusted_issuers: Collection[str] = frozenset(),
) -> LoadedProgressAttestation:
    """Bound, strictly decode, canonicalize, type, and authenticate an envelope."""
    raw = _bounded_source_bytes(source)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ProgressAttestationError("bom_forbidden", "attestation must not contain a BOM")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProgressAttestationError("invalid_utf8", "attestation must be valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except ProgressAttestationError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ProgressAttestationError("invalid_json", "attestation is not valid JSON") from exc
    if type(value) is not dict:
        raise ProgressAttestationError("root_not_object", "attestation root must be an object")
    try:
        canonical = canonical_json_bytes(
            value,
            max_bytes=MAX_PROGRESS_ATTESTATION_BYTES,
            max_nodes=canonical_node_budget_for_bytes(MAX_PROGRESS_ATTESTATION_BYTES),
            max_string_bytes=MAX_PROGRESS_ATTESTATION_BYTES,
        )
    except (CanonicalJsonRefusal, ValueError) as exc:
        raise ProgressAttestationError(
            "noncanonical_json", "attestation JSON is not canonical"
        ) from exc
    if canonical != raw:
        raise ProgressAttestationError(
            "noncanonical_json", "attestation bytes must exactly match canonical JSON"
        )
    try:
        envelope = ProgressEnvelope.model_validate(value)
    except ValidationError as exc:
        raise ProgressAttestationError("schema_invalid", "attestation schema is invalid") from exc
    payload_value = envelope.payload.model_dump(mode="json")
    evidence_core = dict(payload_value)
    evidence_core.pop("issued_at")
    semantic_bytes = canonical_json_bytes(
        payload_value,
        max_bytes=MAX_PROGRESS_ATTESTATION_BYTES,
        max_nodes=canonical_node_budget_for_bytes(MAX_PROGRESS_ATTESTATION_BYTES),
        max_string_bytes=MAX_PROGRESS_ATTESTATION_BYTES,
    )
    semantic_digest = domain_separated_sha256(
        _SEMANTIC_DOMAIN,
        evidence_core,
        max_bytes=MAX_PROGRESS_ATTESTATION_BYTES,
        max_nodes=canonical_node_budget_for_bytes(MAX_PROGRESS_ATTESTATION_BYTES),
        max_string_bytes=MAX_PROGRESS_ATTESTATION_BYTES,
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
            raise ProgressAttestationError(
                "issuer_invalid", "attestation issuer is invalid"
            ) from exc
        if genuine_id != issuer.signer_id:
            raise ProgressAttestationError(
                "issuer_mismatch", "attestation issuer identity mismatches"
            )
        if not trusted:
            raise ProgressAttestationError("issuer_untrusted", "attestation issuer is not trusted")
        if not signing.verify(issuer.public_key, semantic_bytes, issuer.signature):
            raise ProgressAttestationError("signature_invalid", "attestation signature is invalid")
        trust_mode = "configured_issuer_verified"
        issuer_id = genuine_id
    return LoadedProgressAttestation(
        envelope_id=envelope.envelope_id,
        payload=envelope.payload,
        semantic_digest=semantic_digest,
        semantic_bytes=semantic_bytes,
        evidence_core=evidence_core,
        signed_payload=payload_value,
        issuer=(envelope.issuer.model_dump(mode="json") if envelope.issuer is not None else None),
        trust_mode=trust_mode,
        issuer_id=issuer_id,
        raw_size_bytes=len(raw),
    )


def load_progress_attestation_base64(
    value: str,
    *,
    trusted_issuers: Collection[str] = frozenset(),
) -> LoadedProgressAttestation:
    """Strict MCP-safe base64 adapter with the same decoded-byte ceiling."""
    if type(value) is not str or not value:
        raise ProgressAttestationError("base64_invalid", "attestation base64 is invalid")
    max_encoded = ((MAX_PROGRESS_ATTESTATION_BYTES + 2) // 3) * 4
    if len(value) > max_encoded:
        raise ProgressAttestationError("source_too_large", "attestation exceeds the byte limit")
    if any(char.isspace() for char in value):
        raise ProgressAttestationError("base64_invalid", "attestation base64 is invalid")
    if len(value) % 4:
        raise ProgressAttestationError("base64_invalid", "attestation base64 is invalid")
    padding = 2 if value.endswith("==") else 1 if value.endswith("=") else 0
    decoded_size = (len(value) // 4) * 3 - padding
    if decoded_size > MAX_PROGRESS_ATTESTATION_BYTES:
        raise ProgressAttestationError("source_too_large", "attestation exceeds the byte limit")
    try:
        encoded = value.encode("ascii", errors="strict")
        raw = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ProgressAttestationError("base64_invalid", "attestation base64 is invalid") from exc
    if base64.b64encode(raw) != encoded:
        raise ProgressAttestationError("base64_noncanonical", "attestation base64 is not canonical")
    if len(raw) > MAX_PROGRESS_ATTESTATION_BYTES:
        raise ProgressAttestationError("source_too_large", "attestation exceeds the byte limit")
    return load_progress_attestation(raw, trusted_issuers=trusted_issuers)


def verify_progress_attestation(
    loaded: LoadedProgressAttestation,
    context: ClaimProgressContext,
    *,
    project_root: Path,
    now: dt.datetime,
    claim_status: str = "active",
    lease_expires_at: dt.datetime | None = None,
    released_at: dt.datetime | None = None,
) -> VerifiedProgressAttestation:
    """Verify all claim, repository, lifecycle, commit/file, and digest bindings."""
    verified_at = _require_aware_utc(now, "now")
    payload = loaded.payload
    _verify_common_bindings(payload, context, now=verified_at)
    if claim_status != "active" or released_at is not None:
        raise ProgressAttestationError("claim_inactive", "attestation claim is not active")
    if lease_expires_at is not None and verified_at >= _require_aware_utc(
        lease_expires_at, "lease_expires_at"
    ):
        raise ProgressAttestationError("claim_expired", "attestation claim lease has expired")
    local = _local_repository(project_root, project_id=context.project_id)
    if local.repository_id != context.repository_id:
        raise ProgressAttestationError(
            "repository_mismatch", "local repository identity mismatches"
        )
    if local.object_format != context.object_format:
        raise ProgressAttestationError("repository_mismatch", "Git object format mismatches")
    path = canonical_progress_path(payload.path)
    if path != payload.path:
        raise ProgressAttestationError("path_noncanonical", "attestation path is not canonical")
    baseline = {entry.path: entry.baseline_sha256 for entry in context.expected_paths}.get(
        path, _MISSING
    )
    if baseline is _MISSING:
        raise ProgressAttestationError("path_unexpected", "attestation path is outside claim scope")
    if payload.prior_sha256 != baseline:
        raise ProgressAttestationError("baseline_mismatch", "attestation baseline mismatches claim")
    _validate_oid(payload.commit_sha, local.object_format)
    if isinstance(payload, CommitProgressPayload):
        _verify_commit_payload(local.root, payload, context, baseline)
    else:
        if payload.commit_sha != context.claim_start_sha:
            raise ProgressAttestationError(
                "head_moved", "working-tree attestation requires the claim-start HEAD"
            )
        if local.head_oid != payload.commit_sha:
            raise ProgressAttestationError(
                "head_mismatch", "working-tree attestation HEAD mismatches"
            )
        current = _hash_contained_regular_file(local.root, path)
        if current is None:
            raise ProgressAttestationError("file_missing", "attested file does not exist")
        if baseline is not None and not _git_path_changed(
            local.root, context.claim_start_sha, path
        ):
            raise ProgressAttestationError("file_unchanged", "attested file has not changed")
        if current != payload.file_sha256:
            raise ProgressAttestationError("digest_mismatch", "attested file digest mismatches")
        if current == baseline:
            raise ProgressAttestationError("file_unchanged", "attested file has not changed")
    state_payload = {
        "semantic_digest": loaded.semantic_digest,
        "envelope_id": loaded.envelope_id,
        "claim_id": context.claim_id,
        "task_id": context.task_id,
        "claimed_by": context.claimed_by,
        "generation": context.generation,
        "repository_id": context.repository_id,
        "claim_start_sha": context.claim_start_sha,
        "prd_id": context.prd_id,
        "prd_revision": context.prd_revision,
        "task_revision": context.task_revision,
        "kind": payload.kind,
        "commit_sha": payload.commit_sha if payload.kind == "commit" else None,
        "changed_paths": [path] if payload.kind == "commit" else [],
        "path": path if payload.kind == "file" else None,
        "file_sha256": payload.file_sha256 if payload.kind == "file" else None,
        "attested_at": payload.issued_at.isoformat().replace("+00:00", "Z"),
        "recorded_at": verified_at.isoformat().replace("+00:00", "Z"),
        "trust_mode": loaded.trust_mode,
        "issuer_id": loaded.issuer_id,
        "evidence_core": dict(loaded.evidence_core),
        "signed_payload": dict(loaded.signed_payload),
        "issuer": dict(loaded.issuer) if loaded.issuer is not None else None,
    }
    return VerifiedProgressAttestation(loaded=loaded, state_payload=state_payload)


@dataclass(frozen=True, slots=True)
class _LocalRepository:
    root: Path
    common_dir: Path
    object_format: Literal["sha1", "sha256"]
    head_oid: str
    repository_id: str


_MISSING = object()


def _bounded_source_bytes(source: bytes | BinaryIO) -> bytes:
    if isinstance(source, bytes):
        raw = source
    elif hasattr(source, "read"):
        buffered = bytearray()
        try:
            while len(buffered) <= MAX_PROGRESS_ATTESTATION_BYTES:
                remaining = MAX_PROGRESS_ATTESTATION_BYTES + 1 - len(buffered)
                chunk = source.read(remaining)
                if not isinstance(chunk, bytes):
                    raise ProgressAttestationError(
                        "source_invalid", "attestation stream must be binary"
                    )
                if not chunk:
                    break
                buffered.extend(chunk)
        except ProgressAttestationError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise ProgressAttestationError("source_unavailable", "cannot read attestation") from exc
        raw = bytes(buffered)
    else:
        raise ProgressAttestationError("source_invalid", "attestation source must be bytes")
    if len(raw) > MAX_PROGRESS_ATTESTATION_BYTES:
        raise ProgressAttestationError("source_too_large", "attestation exceeds the byte limit")
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProgressAttestationError("duplicate_key", "attestation contains a duplicate key")
        result[key] = value
    return result


def _reject_float(_value: str) -> Any:
    raise ProgressAttestationError("float_forbidden", "attestation cannot contain floats")


def _reject_constant(_value: str) -> Any:
    raise ProgressAttestationError(
        "constant_forbidden", "attestation cannot contain non-finite values"
    )


def _bounded_identity(value: str, name: str, *, max_bytes: int = 255) -> str:
    if type(value) is not str or not value.strip():
        raise ProgressAttestationError("context_invalid", f"{name} is invalid")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ProgressAttestationError("context_invalid", f"{name} is invalid") from exc
    if size > max_bytes:
        raise ProgressAttestationError("context_invalid", f"{name} is outside size limits")
    return value


def _positive_int(value: int, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ProgressAttestationError("context_invalid", f"{name} must be a positive integer")
    return value


def _require_aware_utc(value: dt.datetime, name: str) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProgressAttestationError("time_invalid", f"{name} must be timezone-aware")
    return value.astimezone(dt.UTC)


def _canonical_expected_paths(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not 0 <= len(values) <= MAX_PROGRESS_PATHS:
        raise ProgressAttestationError(
            "expected_paths_invalid", "expected paths are outside limits"
        )
    paths = tuple(canonical_progress_path(value) for value in values)
    normalized = tuple(os.path.normcase(path) for path in paths)
    if len(normalized) != len(set(normalized)):
        raise ProgressAttestationError("expected_paths_duplicate", "expected paths are not unique")
    return paths


def _json_snapshot(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    return value


def _verify_common_bindings(
    payload: CommitProgressPayload | FileProgressPayload,
    context: ClaimProgressContext,
    *,
    now: dt.datetime,
) -> None:
    expected = (
        (payload.project_id, context.project_id),
        (payload.claim_id, context.claim_id),
        (payload.generation, context.generation),
        (payload.task_id, context.task_id),
        (payload.task_revision, context.task_revision),
        (payload.prd_id, context.prd_id),
        (payload.prd_revision, context.prd_revision),
        (payload.claimed_by, context.claimed_by),
        (payload.repository_id, context.repository_id),
        (payload.claim_start_sha, context.claim_start_sha),
    )
    if any(actual != bound for actual, bound in expected):
        raise ProgressAttestationError(
            "claim_binding_mismatch", "attestation claim binding mismatches"
        )
    if payload.issued_at < context.claim_created_at:
        raise ProgressAttestationError("issued_before_claim", "attestation predates the claim")
    if payload.issued_at > now:
        raise ProgressAttestationError("issued_in_future", "attestation issuance is in the future")


def _local_repository(project_root: Path, *, project_id: str) -> _LocalRepository:
    try:
        requested = Path(project_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProgressAttestationError(
            "repository_unavailable", "project repository path is unavailable"
        ) from exc
    try:
        top = Path(_git_text(requested, "rev-parse", "--show-toplevel")).resolve(strict=True)
    except ProgressAttestationError as exc:
        if exc.code != "git_verification_failed":
            raise
        raise ProgressAttestationError(
            "repository_unavailable", "project path is not a Git worktree"
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise ProgressAttestationError(
            "repository_unavailable", "Git worktree root is unavailable"
        ) from exc
    common_raw = _git_text(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common_path = Path(common_raw)
    if not common_path.is_absolute():
        common_path = top / common_path
    try:
        common = common_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProgressAttestationError(
            "repository_unavailable", "Git common directory is unavailable"
        ) from exc
    object_format = _git_text(top, "rev-parse", "--show-object-format")
    if object_format not in {"sha1", "sha256"}:
        raise ProgressAttestationError("git_object_format", "unsupported Git object format")
    head = _git_text(top, "rev-parse", "--verify", "HEAD^{commit}")
    _validate_oid(head, object_format)
    canonical_common = _canonical_os_identity(common)
    repo_id = domain_separated_sha256(
        _REPOSITORY_DOMAIN,
        {
            "git_common_dir": canonical_common,
            "project_id": _bounded_identity(project_id, "project_id"),
        },
        max_bytes=16_384,
        max_string_bytes=8_192,
    )
    return _LocalRepository(
        root=top,
        common_dir=common,
        object_format=object_format,
        head_oid=head,
        repository_id=repo_id,
    )


def inspect_local_repository(project_root: Path, *, project_id: str) -> _LocalRepository:
    """Return the canonical local Git identity used by claim-bound verifiers."""
    return _local_repository(project_root, project_id=project_id)


def _canonical_os_identity(path: Path) -> str:
    value = str(path.resolve(strict=True))
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    value = value.replace("\\", "/")
    return value.casefold() if os.name == "nt" else value


def _validate_oid(value: str, object_format: str) -> None:
    length = 40 if object_format == "sha1" else 64
    if len(value) != length or not re.fullmatch(r"[0-9a-f]+", value):
        raise ProgressAttestationError(
            "commit_invalid", "commit id is not a full canonical object id"
        )


def _git_text(root: Path, *args: str) -> str:
    raw = _run_git(root, *args, output_limit=MAX_GIT_OUTPUT_BYTES)
    try:
        value = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ProgressAttestationError("git_output_invalid", "Git returned invalid UTF-8") from exc
    if not value or "\x00" in value:
        raise ProgressAttestationError("git_output_invalid", "Git returned invalid output")
    return value


def _run_git(
    root: Path,
    *args: str,
    output_limit: int = MAX_GIT_OUTPUT_BYTES,
    accepted_codes: Collection[int] = (0,),
) -> bytes:
    command = ["git", "-C", str(root), *args]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError as exc:
        raise ProgressAttestationError("git_unavailable", "cannot execute Git") from exc
    output = bytearray()
    problem: list[BaseException] = []

    def read_stdout() -> None:
        assert process.stdout is not None
        try:
            while len(output) <= output_limit:
                chunk = process.stdout.read(min(65_536, output_limit + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
        except BaseException as exc:  # pragma: no cover - OS pipe failure
            problem.append(exc)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    try:
        return_code = process.wait(timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        reader.join(timeout=1)
        raise ProgressAttestationError("git_timeout", "Git verification timed out") from exc
    reader.join(timeout=1)
    if reader.is_alive():
        process.kill()
        raise ProgressAttestationError("git_timeout", "Git output did not close")
    if problem:
        raise ProgressAttestationError("git_unavailable", "cannot read Git output") from problem[0]
    if len(output) > output_limit:
        raise ProgressAttestationError("git_output_limit", "Git output exceeds the verifier limit")
    if return_code not in accepted_codes:
        raise ProgressAttestationError("git_verification_failed", "Git verification failed")
    return bytes(output)


def _verify_commit_payload(
    root: Path,
    payload: CommitProgressPayload,
    context: ClaimProgressContext,
    baseline: object,
) -> None:
    resolved = _git_text(root, "rev-parse", "--verify", f"{payload.commit_sha}^{{commit}}")
    if resolved != payload.commit_sha:
        raise ProgressAttestationError("commit_invalid", "attested commit does not resolve exactly")
    if payload.commit_sha == context.claim_start_sha:
        raise ProgressAttestationError("commit_unchanged", "attested commit equals claim start")
    _run_git(
        root,
        "merge-base",
        "--is-ancestor",
        context.claim_start_sha,
        payload.commit_sha,
    )
    start_entry = _git_tree_entry(root, context.claim_start_sha, payload.path)
    commit_entry = _git_tree_entry(root, payload.commit_sha, payload.path)
    if commit_entry is None or commit_entry[0] not in {"100644", "100755"}:
        raise ProgressAttestationError(
            "commit_path_not_blob", "attested path is not a regular Git blob"
        )
    if start_entry is not None and start_entry[0] not in {"100644", "100755"}:
        raise ProgressAttestationError(
            "baseline_not_blob", "claim-start path is not a regular Git blob"
        )
    start_digest = None if start_entry is None else _hash_git_blob(root, start_entry[1])
    if start_digest != baseline:
        raise ProgressAttestationError("baseline_mismatch", "Git baseline mismatches claim capture")
    commit_digest = _hash_git_blob(root, commit_entry[1])
    if commit_digest != payload.file_sha256:
        raise ProgressAttestationError("digest_mismatch", "attested commit blob digest mismatches")
    if commit_digest == start_digest:
        raise ProgressAttestationError("file_unchanged", "attested path has not changed")
    _run_git(
        root,
        "diff",
        "--quiet",
        context.claim_start_sha,
        payload.commit_sha,
        "--",
        f":(literal){payload.path}",
        accepted_codes=(1,),
    )


def _capture_expected_baseline(
    local: _LocalRepository,
    path: str,
    *,
    claim_start_sha: str | None = None,
    check_worktree: bool = True,
) -> str | None:
    """Capture a clean claim-start Git blob identity without line-ending drift."""
    baseline_sha = claim_start_sha or local.head_oid
    entry = _git_tree_entry(local.root, baseline_sha, path)
    if not check_worktree:
        if entry is None:
            return None
        if entry[0] not in {"100644", "100755"}:
            raise ProgressAttestationError(
                "baseline_not_blob", "claim-start path is not a regular Git blob"
            )
        return _hash_git_blob(local.root, entry[1])
    working_digest = _hash_contained_regular_file(local.root, path)
    if entry is None:
        if working_digest is not None:
            raise ProgressAttestationError(
                "claim_path_dirty", "untracked expected file exists at claim start"
            )
        return None
    if entry[0] not in {"100644", "100755"}:
        raise ProgressAttestationError(
            "baseline_not_blob", "claim-start path is not a regular Git blob"
        )
    if working_digest is None:
        raise ProgressAttestationError(
            "claim_path_dirty", "tracked expected file is absent at claim start"
        )
    if _git_path_changed(local.root, baseline_sha, path):
        raise ProgressAttestationError(
            "claim_path_dirty", "expected file is modified at claim start"
        )
    return _hash_git_blob(local.root, entry[1])


def _git_path_changed(root: Path, baseline: str, path: str) -> bool:
    code = _git_exit_code(
        root,
        "diff",
        "--quiet",
        baseline,
        "--",
        f":(literal){path}",
    )
    if code not in {0, 1}:
        raise ProgressAttestationError("git_verification_failed", "Git diff verification failed")
    return code == 1


def _git_exit_code(root: Path, *args: str) -> int:
    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError as exc:
        raise ProgressAttestationError("git_unavailable", "cannot execute Git") from exc
    try:
        return process.wait(timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise ProgressAttestationError("git_timeout", "Git verification timed out") from exc


def _git_tree_entry(root: Path, commit: str, path: str) -> tuple[str, str] | None:
    raw = _run_git(
        root,
        "ls-tree",
        "-z",
        "--full-tree",
        commit,
        "--",
        f":(literal){path}",
    )
    if raw == b"":
        return None
    records = raw.split(b"\0")
    if records[-1] != b"" or len(records) != 2:
        raise ProgressAttestationError("git_tree_invalid", "Git tree lookup is ambiguous")
    try:
        metadata, returned = records[0].split(b"\t", 1)
        mode, object_type, oid = metadata.decode("ascii").split(" ")
        returned_path = returned.decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProgressAttestationError("git_tree_invalid", "Git tree lookup is invalid") from exc
    if object_type != "blob" or returned_path != path or not re.fullmatch(r"[0-9a-f]+", oid):
        raise ProgressAttestationError("git_tree_invalid", "Git tree lookup mismatches")
    return mode, oid


def _hash_git_blob(root: Path, oid: str) -> str:
    # Blob content can be arbitrarily large, so this path hashes without
    # retaining output.  Metadata commands use the capped ``_run_git`` helper.
    return _stream_git_blob(root, oid)


def _stream_git_blob(root: Path, oid: str) -> str:
    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), "cat-file", "blob", oid],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError as exc:
        raise ProgressAttestationError("git_unavailable", "cannot execute Git") from exc
    digest = hashlib.sha256()
    problem: list[BaseException] = []

    def hash_stdout() -> None:
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read(1024 * 1024):
                digest.update(chunk)
        except BaseException as exc:  # pragma: no cover - OS pipe failure
            problem.append(exc)

    reader = threading.Thread(target=hash_stdout, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        reader.join(timeout=1)
        raise ProgressAttestationError("git_timeout", "Git blob verification timed out") from exc
    reader.join(timeout=1)
    if code != 0 or problem or reader.is_alive():
        process.kill()
        raise ProgressAttestationError("git_verification_failed", "Git blob verification failed")
    return digest.hexdigest()


def _hash_contained_regular_file(root: Path, portable_path: str) -> str | None:
    canonical = canonical_progress_path(portable_path)
    resolved_root = root.resolve(strict=True)
    return (
        _hash_file_windows(resolved_root, canonical)
        if os.name == "nt"
        else _hash_file_posix(resolved_root, canonical)
    )


def _hash_file_posix(root: Path, portable_path: str) -> str | None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    for name in ("O_CLOEXEC", "O_NOFOLLOW"):
        directory_flags |= getattr(os, name, 0)
    file_flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        file_flags |= getattr(os, name, 0)
    descriptors: list[int] = []
    try:
        current = os.open(root, directory_flags)
        descriptors.append(current)
        parts = portable_path.split("/")
        for segment in parts[:-1]:
            current = os.open(segment, directory_flags, dir_fd=current)
            descriptors.append(current)
        descriptor = os.open(parts[-1], file_flags, dir_fd=current)
        descriptors.append(descriptor)
        return _hash_stable_descriptor(descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        code = "path_link" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "file_unavailable"
        raise ProgressAttestationError(code, "cannot safely read expected file") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _hash_file_windows(root: Path, portable_path: str) -> str | None:
    candidate = root.joinpath(*portable_path.split("/"))
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    current = root
    try:
        for part in portable_path.split("/"):
            current = current / part
            inspected = os.lstat(current)
            if getattr(inspected, "st_file_attributes", 0) & reparse_flag:
                raise ProgressAttestationError(
                    "path_link", "expected file path crosses a reparse point"
                )
        before = os.lstat(candidate)
        if not stat.S_ISREG(before.st_mode):
            raise ProgressAttestationError("file_not_regular", "expected file is not regular")
        descriptor = os.open(
            candidate,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
        )
    except FileNotFoundError:
        return None
    except ProgressAttestationError:
        raise
    except OSError as exc:
        raise ProgressAttestationError(
            "file_unavailable", "cannot safely open expected file"
        ) from exc
    try:
        final_path = _windows_final_path_for_descriptor(descriptor).resolve(strict=True)
        expected = candidate.resolve(strict=True)
        if os.path.normcase(str(final_path)) != os.path.normcase(str(expected)):
            raise ProgressAttestationError(
                "path_link", "open file path changed during verification"
            )
        try:
            final_path.relative_to(root)
        except ValueError as exc:
            raise ProgressAttestationError(
                "path_escape", "expected file escapes project root"
            ) from exc
        digest = _hash_stable_descriptor(descriptor)
        after = os.lstat(candidate)
        if not os.path.samestat(before, after):
            raise ProgressAttestationError(
                "file_changed", "expected file changed during verification"
            )
        return digest
    finally:
        os.close(descriptor)


def _hash_stable_descriptor(descriptor: int) -> str:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ProgressAttestationError("file_not_regular", "expected file is not regular")
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    after = os.fstat(descriptor)
    if (
        not os.path.samestat(before, after)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise ProgressAttestationError("file_changed", "expected file changed during verification")
    return digest.hexdigest()


def _windows_final_path_for_descriptor(descriptor: int) -> Path:
    if os.name != "nt":  # pragma: no cover - guarded by caller
        raise OSError("Windows handle path resolution is unavailable")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    function = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = function(handle, buffer, size, 0)
        if length == 0:
            raise OSError(ctypes.get_last_error(), "cannot resolve open file handle")
        if length < size:
            value = buffer.value
            break
        size = length + 1
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


__all__ = [
    "MAX_PROGRESS_ATTESTATION_BYTES",
    "ClaimProgressContext",
    "CommitProgressPayload",
    "FileProgressPayload",
    "LoadedProgressAttestation",
    "PathBaseline",
    "ProgressAttestationError",
    "VerifiedProgressAttestation",
    "canonical_progress_path",
    "capture_claim_progress_context",
    "load_progress_attestation",
    "load_progress_attestation_base64",
    "verify_progress_attestation",
]
