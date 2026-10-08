"""Complete bounded hook-buffer inspection, shared by every evidence adapter."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from anvil.naming import task_claim_buffer_path
from anvil.state.models import (
    MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES,
    MAX_CLAIM_COMMAND_PROOF_BATCH_ITEMS,
    CommandProof,
    HookCommandAttribution,
)


class CommandProofImportOverflow(ValueError):
    """Compatibility refusal for any import whose completeness is unproved."""

    code = "command_proof_import_overflow"

    def __init__(
        self, message: str, *, inspection: CommandBufferInspection | None = None,
    ) -> None:
        super().__init__(message)
        self.inspection = inspection


@dataclass(frozen=True)
class CommandBufferInspection:
    """Full inspection facts; missing optional input has no content digest."""

    status: str
    source_sha256: str | None
    inspected_bytes: int
    inspected_records: int
    skipped: tuple[tuple[str, int], ...]
    proofs: tuple[CommandProof, ...]


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_buffer(path: Path, max_bytes: int) -> bytes | None:
    descriptor = -1
    try:
        try:
            directory = path.parent.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(directory.st_mode):
            raise CommandProofImportOverflow("command proof buffer directory is invalid")
        try:
            before = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            if _identity(path.parent.stat(follow_symlinks=False)) != _identity(directory):
                raise CommandProofImportOverflow(
                    "command proof buffer directory changed during inspection"
                ) from None
            return None
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0:
            raise CommandProofImportOverflow("command proof buffer is not a regular file")
        if before.st_size > max_bytes:
            raise CommandProofImportOverflow("command proof buffer exceeds its byte limit")
        flags = os.O_RDONLY
        for name in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT", "O_NONBLOCK", "O_NOFOLLOW"):
            flags |= getattr(os, name, 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise CommandProofImportOverflow("command proof buffer changed before complete import")
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        with stream:
            data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise CommandProofImportOverflow("command proof buffer exceeds its byte limit")
            if len(data) != before.st_size:
                raise CommandProofImportOverflow("command proof buffer cannot be read completely")
            stream.seek(0)
            repeated = stream.read(max_bytes + 1)
            if (
                data != repeated
                or _identity(os.fstat(stream.fileno())) != _identity(before)
                or _identity(path.stat(follow_symlinks=False)) != _identity(before)
                or _identity(path.parent.stat(follow_symlinks=False)) != _identity(directory)
            ):
                raise CommandProofImportOverflow(
                    "command proof buffer changed during complete import"
                )
        return data
    except OSError as exc:
        raise CommandProofImportOverflow("command proof buffer cannot be read completely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def inspect_command_buffer(
    state_dir: Path,
    claim_id: str,
    *,
    max_bytes: int = MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES,
) -> CommandBufferInspection:
    """Inspect exact EOF or refuse; historical/invalid records remain skipped.

    Limits apply to the entire source and all valid proofs, including failures.
    Skipped reasons are fixed vocabulary/counts, never source text or errors.
    """
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_CLAIM_COMMAND_PROOF_BATCH_BYTES:
        raise CommandProofImportOverflow("command proof buffer byte limit is invalid")
    path = task_claim_buffer_path(state_dir / ".evidence-buffer", claim_id)
    if path is None:
        # Legacy claim IDs have no supported hook-buffer filename. Explicit
        # claim-bound artifacts still work; discovery must label this boundary.
        return CommandBufferInspection("ineligible", None, 0, 0, (), ())
    data = _read_buffer(path, max_bytes)
    if data is None:
        return CommandBufferInspection("missing", None, 0, 0, (), ())
    source_sha256 = hashlib.sha256(data).hexdigest()
    proofs: list[CommandProof] = []
    skipped: dict[str, int] = {}
    records = 0
    # ponytail: whole-source scan capped at 1 MiB; no prefix or suffix import.
    for raw in io.BytesIO(data):
        records += 1
        reason = "invalid"
        try:
            line = raw.decode("utf-8").strip()
            if not line:
                reason = "empty"
            else:
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    reason = "not_object"
                elif rec.get("claim_id") != claim_id:
                    reason = "other_claim"
                else:
                    attribution = HookCommandAttribution.model_validate(rec["attribution"])
                    if attribution.claim_id != claim_id:
                        reason = "other_claim"
                    else:
                        proof = CommandProof(
                            command=rec["command"],
                            exit_code=rec["exit_code"],
                            output_sha256=rec["output_sha256"],
                            captured_at=datetime.fromisoformat(rec["timestamp"]),
                            attribution=attribution,
                            semantic_digest=rec["semantic_digest"],
                        )
                        if len(proofs) == MAX_CLAIM_COMMAND_PROOF_BATCH_ITEMS:
                            raise CommandProofImportOverflow(
                                "command proof buffer exceeds its record limit",
                                inspection=CommandBufferInspection(
                                    "incomplete", source_sha256, len(data), records,
                                    tuple(sorted(skipped.items())), (),
                                ),
                            )
                        proofs.append(proof)
                        continue
        except CommandProofImportOverflow:
            raise
        except (
            UnicodeError, json.JSONDecodeError, KeyError, RecursionError, ValueError, TypeError,
        ):
            pass
        skipped[reason] = skipped.get(reason, 0) + 1
    return CommandBufferInspection(
        "complete", source_sha256, len(data), records,
        tuple(sorted(skipped.items())), tuple(proofs),
    )


def require_buffer_unchanged(
    state_dir: Path, claim_id: str, expected: CommandBufferInspection,
) -> None:
    """Final append callback; supported writers hold the same engine lock."""
    current = inspect_command_buffer(state_dir, claim_id)
    if current != expected:
        raise CommandProofImportOverflow("command proof buffer changed before submission")
