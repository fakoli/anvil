"""Focused security and round-trip tests for claim progress attestations."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from anvil import signing
from anvil.claims.progress_attestation import (
    MAX_PROGRESS_ATTESTATION_BYTES,
    ClaimProgressContext,
    ProgressAttestationError,
    canonical_progress_path,
    capture_claim_progress_context,
    load_progress_attestation,
    load_progress_attestation_base64,
    verify_progress_attestation,
)
from anvil.state.hashing import canonical_json_bytes
from anvil.state.payloads import ProgressAttestedPayload

_NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Anvil Tests")
    (root / "src").mkdir()
    (root / "src" / "feature.txt").write_bytes(b"before\n")
    _git(root, "add", "--", "src/feature.txt")
    _git(root, "commit", "-m", "initial")
    return root


def _context(repo: Path) -> ClaimProgressContext:
    return capture_claim_progress_context(
        repo,
        project_id="P-1",
        claim_id="CL-1",
        claim_generation=2,
        task_id="T007",
        task_snapshot={"id": "T007", "title": "Verify progress", "revision": 3},
        prd_id="autonomous-lifecycle-hardening",
        prd_revision=4,
        actor="agent/exact",
        claim_created_at=_NOW,
        expected_paths=["src\\feature.txt"],
    )


def _payload(
    context: ClaimProgressContext,
    *,
    kind: str,
    commit_sha: str,
    file_sha256: str,
) -> dict[str, object]:
    baseline = context.expected_paths[0]
    return {
        "claim_id": context.claim_id,
        "claim_start_sha": context.claim_start_sha,
        "claimed_by": context.claimed_by,
        "commit_sha": commit_sha,
        "file_sha256": file_sha256,
        "generation": context.generation,
        "issued_at": (_NOW + dt.timedelta(seconds=1))
        .isoformat()
        .replace("+00:00", "Z"),
        "kind": kind,
        "path": baseline.path,
        "prd_id": context.prd_id,
        "prd_revision": context.prd_revision,
        "prior_sha256": baseline.baseline_sha256,
        "project_id": context.project_id,
        "repository_id": context.repository_id,
        "schema_version": 1,
        "task_id": context.task_id,
        "task_revision": context.task_revision,
    }


def _envelope_bytes(
    payload: dict[str, object],
    *,
    envelope_id: str = "ENV-1",
    issuer: dict[str, str] | None = None,
) -> bytes:
    envelope: dict[str, object] = {"envelope_id": envelope_id, "payload": payload}
    if issuer is not None:
        envelope["issuer"] = issuer
    return canonical_json_bytes(
        envelope,
        max_bytes=MAX_PROGRESS_ATTESTATION_BYTES,
        max_string_bytes=MAX_PROGRESS_ATTESTATION_BYTES,
    )


def _assert_code(code: str, call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ProgressAttestationError) as caught:
        call()
    assert caught.value.code == code


def test_context_round_trip_is_verbatim_state_shape(repo: Path) -> None:
    context = _context(repo)
    durable = context.context_dict()

    assert set(durable) == {
        "repository_id",
        "claim_start_sha",
        "prd_id",
        "prd_revision",
        "task_revision",
        "expected_paths",
    }
    assert durable["expected_paths"] == [
        {
            "path": "src/feature.txt",
            "baseline_sha256": hashlib.sha256(b"before\n").hexdigest(),
        }
    ]
    rebound = ClaimProgressContext.from_context_dict(
        durable,
        project_id=context.project_id,
        claim_id=context.claim_id,
        generation=context.generation,
        task_id=context.task_id,
        claimed_by=context.claimed_by,
        claim_created_at=context.claim_created_at,
    )
    assert rebound == context
    assert rebound.model_dump() == durable


def test_context_allows_empty_scope_but_no_attestation_can_target_it(
    repo: Path,
) -> None:
    context = capture_claim_progress_context(
        repo,
        project_id="P-1",
        claim_id="CL-empty",
        claim_generation=1,
        task_id="T-empty",
        task_snapshot={"id": "T-empty"},
        prd_id="prd",
        prd_revision=1,
        actor="agent",
        claim_created_at=_NOW,
        expected_paths=[],
    )
    assert context.expected_paths == ()
    assert context.context_dict()["expected_paths"] == []


@pytest.mark.skipif(os.name != "nt", reason="case aliases are platform-specific")
def test_context_refuses_windows_case_aliases(repo: Path) -> None:
    with pytest.raises(ProgressAttestationError) as caught:
        capture_claim_progress_context(
            repo,
            project_id="P-1",
            claim_id="CL-alias",
            claim_generation=1,
            task_id="T-alias",
            task_snapshot={"id": "T-alias"},
            prd_id="prd",
            prd_revision=1,
            actor="agent",
            claim_created_at=_NOW,
            expected_paths=["src/feature.txt", "SRC/FEATURE.TXT"],
        )
    assert caught.value.code == "expected_paths_duplicate"


def test_repository_identity_is_shared_by_linked_worktrees(
    repo: Path, tmp_path: Path
) -> None:
    worktree = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    first = _context(repo)
    second = capture_claim_progress_context(
        worktree,
        project_id=first.project_id,
        claim_id="CL-2",
        claim_generation=1,
        task_id="T007",
        task_snapshot={"id": "T007"},
        prd_id="prd",
        prd_revision=1,
        actor="agent",
        claim_created_at=_NOW,
        expected_paths=[],
    )
    assert second.repository_id == first.repository_id
    assert second.claim_start_sha == first.claim_start_sha


def test_context_refuses_preclaim_dirty_or_untracked_expected_files(repo: Path) -> None:
    (repo / "src" / "feature.txt").write_bytes(b"already modified\n")
    with pytest.raises(ProgressAttestationError) as dirty:
        _context(repo)
    assert dirty.value.code == "claim_path_dirty"

    _git(repo, "checkout", "--", "src/feature.txt")
    (repo / "src" / "new.txt").write_bytes(b"already present\n")
    with pytest.raises(ProgressAttestationError) as untracked:
        capture_claim_progress_context(
            repo,
            project_id="P-1",
            claim_id="CL-new",
            claim_generation=1,
            task_id="T-new",
            task_snapshot={"id": "T-new"},
            prd_id="prd",
            prd_revision=1,
            actor="agent",
            claim_created_at=_NOW,
            expected_paths=["src/new.txt"],
        )
    assert untracked.value.code == "claim_path_dirty"


def test_git_blob_baseline_avoids_autocrlf_false_progress(repo: Path) -> None:
    _git(repo, "config", "core.autocrlf", "true")
    (repo / "src" / "feature.txt").unlink()
    _git(repo, "checkout", "--", "src/feature.txt")
    raw = (repo / "src" / "feature.txt").read_bytes()
    context = _context(repo)
    assert (
        context.expected_paths[0].baseline_sha256
        == hashlib.sha256(b"before\n").hexdigest()
    )

    loaded = load_progress_attestation(
        _envelope_bytes(
            _payload(
                context,
                kind="file",
                commit_sha=context.claim_start_sha,
                file_sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    )
    _assert_code(
        "file_unchanged",
        lambda: verify_progress_attestation(
            loaded,
            context,
            project_root=repo,
            now=_NOW + dt.timedelta(seconds=2),
        ),
    )


def test_file_attestation_accepts_expected_file_created_after_claim(repo: Path) -> None:
    context = capture_claim_progress_context(
        repo,
        project_id="P-1",
        claim_id="CL-new",
        claim_generation=1,
        task_id="T-new",
        task_snapshot={"id": "T-new"},
        prd_id="prd",
        prd_revision=1,
        actor="agent",
        claim_created_at=_NOW,
        expected_paths=["src/new.txt"],
    )
    assert context.expected_paths[0].baseline_sha256 is None
    content = b"created after claim\n"
    (repo / "src" / "new.txt").write_bytes(content)
    loaded = load_progress_attestation(
        _envelope_bytes(
            _payload(
                context,
                kind="file",
                commit_sha=context.claim_start_sha,
                file_sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    )
    verified = verify_progress_attestation(
        loaded,
        context,
        project_root=repo,
        now=_NOW + dt.timedelta(seconds=2),
    )
    assert verified.state_payload["path"] == "src/new.txt"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("src/feature.txt", "src/feature.txt"),
        ("src\\feature.txt", "src/feature.txt"),
        ("./src/feature.txt", "src/feature.txt"),
        ("café/文件.txt", "café/文件.txt"),
    ],
)
def test_canonical_progress_path_portable(value: str, expected: str) -> None:
    assert canonical_progress_path(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute",
        "\\absolute",
        "C:\\absolute",
        "\\\\server\\share",
        "\\\\?\\C:\\device",
        "a//b",
        "a\\\\b",
        "../escape",
        "a/../b",
        "a:stream",
        "a/b.",
        "a/b ",
        "CON",
        "con.txt",
        "LPT1/x",
        "COM¹.txt",
        "a\x00b",
        "a\x85b",
        "a?b",
        "\ud800",
    ],
)
def test_canonical_progress_path_refuses_unsafe_cross_platform_forms(
    value: str,
) -> None:
    with pytest.raises(ProgressAttestationError):
        canonical_progress_path(value)


def test_loader_reads_limit_plus_one_before_decode() -> None:
    class RecordingStream(io.BytesIO):
        requested: int | None = None

        def read(self, size: int = -1) -> bytes:
            self.requested = size
            return super().read(size)

    stream = RecordingStream(b"x" * (MAX_PROGRESS_ATTESTATION_BYTES + 1))
    _assert_code("source_too_large", lambda: load_progress_attestation(stream))
    assert stream.requested == MAX_PROGRESS_ATTESTATION_BYTES + 1


def test_loader_collects_short_reads_and_detects_hidden_overrun(repo: Path) -> None:
    context = _context(repo)
    raw = _envelope_bytes(
        _payload(
            context,
            kind="file",
            commit_sha=context.claim_start_sha,
            file_sha256="a" * 64,
        )
    )

    class ChunkedStream:
        def __init__(self, content: bytes, chunk_size: int) -> None:
            self.content = content
            self.chunk_size = chunk_size
            self.offset = 0
            self.calls = 0

        def read(self, size: int = -1) -> bytes:
            self.calls += 1
            count = min(size, self.chunk_size, len(self.content) - self.offset)
            if count <= 0:
                return b""
            result = self.content[self.offset : self.offset + count]
            self.offset += count
            return result

    chunked = ChunkedStream(raw, 7)
    loaded = load_progress_attestation(chunked)  # type: ignore[arg-type]
    assert loaded.raw_size_bytes == len(raw)
    assert chunked.calls > 2

    hidden = ChunkedStream(
        raw + b"x" * (MAX_PROGRESS_ATTESTATION_BYTES + 1 - len(raw)),
        MAX_PROGRESS_ATTESTATION_BYTES,
    )
    _assert_code("source_too_large", lambda: load_progress_attestation(hidden))  # type: ignore[arg-type]
    assert hidden.calls == 2


def test_loader_treats_zero_progress_as_eof_without_looping() -> None:
    class ZeroThenData:
        calls = 0

        def read(self, size: int = -1) -> bytes:
            self.calls += 1
            return b"" if self.calls == 1 else b"{}"

    stream = ZeroThenData()
    _assert_code("invalid_json", lambda: load_progress_attestation(stream))  # type: ignore[arg-type]
    assert stream.calls == 1


def test_base64_adapter_is_strict_bounded_and_preserves_hostile_bytes() -> None:
    _assert_code("base64_invalid", lambda: load_progress_attestation_base64("e30=\n"))
    _assert_code("base64_invalid", lambda: load_progress_attestation_base64("e30"))
    too_long = "A" * ((((MAX_PROGRESS_ATTESTATION_BYTES + 2) // 3) * 4) + 1)
    _assert_code("source_too_large", lambda: load_progress_attestation_base64(too_long))
    decoded_overrun_without_padding = "A" * (
        ((MAX_PROGRESS_ATTESTATION_BYTES + 2) // 3) * 4
    )
    _assert_code(
        "source_too_large",
        lambda: load_progress_attestation_base64(decoded_overrun_without_padding),
    )
    _assert_code(
        "bom_forbidden",
        lambda: load_progress_attestation_base64(
            base64.b64encode(b"\xef\xbb\xbf{}").decode()
        ),
    )
    _assert_code(
        "invalid_utf8",
        lambda: load_progress_attestation_base64(base64.b64encode(b"\xff").decode()),
    )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"\xef\xbb\xbf{}", "bom_forbidden"),
        (b"\xff", "invalid_utf8"),
        (b"[]", "root_not_object"),
        (b'{"a":1,"a":2}', "duplicate_key"),
        (b'{"a":1.0}', "float_forbidden"),
        (b'{"a":NaN}', "constant_forbidden"),
        (b'{"envelope_id":"x","payload":{"x":"\\ud800"}}', "noncanonical_json"),
    ],
)
def test_loader_refuses_hostile_json_boundaries(raw: bytes, code: str) -> None:
    _assert_code(code, lambda: load_progress_attestation(raw))


def test_loader_requires_exact_canonical_bytes_and_forbids_unknown_fields(
    repo: Path,
) -> None:
    context = _context(repo)
    payload = _payload(
        context,
        kind="file",
        commit_sha=context.claim_start_sha,
        file_sha256="a" * 64,
    )
    canonical = _envelope_bytes(payload)
    _assert_code(
        "noncanonical_json", lambda: load_progress_attestation(canonical + b"\n")
    )
    value = json.loads(canonical)
    value["unknown"] = True
    _assert_code(
        "schema_invalid",
        lambda: load_progress_attestation(canonical_json_bytes(value)),
    )
    invalid_version = dict(payload)
    invalid_version["schema_version"] = True
    _assert_code(
        "schema_invalid",
        lambda: load_progress_attestation(_envelope_bytes(invalid_version)),
    )
    numeric_time = dict(payload)
    numeric_time["issued_at"] = 1
    _assert_code(
        "schema_invalid",
        lambda: load_progress_attestation(_envelope_bytes(numeric_time)),
    )


def test_semantic_digest_excludes_envelope_and_signature_wrapper(repo: Path) -> None:
    context = _context(repo)
    payload = _payload(
        context,
        kind="file",
        commit_sha=context.claim_start_sha,
        file_sha256="a" * 64,
    )
    first = load_progress_attestation(_envelope_bytes(payload, envelope_id="ENV-A"))
    second = load_progress_attestation(_envelope_bytes(payload, envelope_id="ENV-B"))
    assert first.semantic_digest == second.semantic_digest
    assert first.semantic_bytes == second.semantic_bytes


def test_evidence_digest_is_stable_across_issued_at_but_signature_preimage_is_not(
    repo: Path,
) -> None:
    context = _context(repo)
    payload = _payload(
        context,
        kind="file",
        commit_sha=context.claim_start_sha,
        file_sha256="a" * 64,
    )
    later = dict(payload)
    later["issued_at"] = (
        (_NOW + dt.timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    )

    first = load_progress_attestation(_envelope_bytes(payload))
    second = load_progress_attestation(_envelope_bytes(later))

    assert first.evidence_core == second.evidence_core
    assert first.semantic_digest == second.semantic_digest
    assert first.signed_payload != second.signed_payload
    assert first.semantic_bytes != second.semantic_bytes


def test_asserted_issuer_must_be_genuine_trusted_and_valid(
    repo: Path, tmp_path: Path
) -> None:
    context = _context(repo)
    payload = _payload(
        context,
        kind="file",
        commit_sha=context.claim_start_sha,
        file_sha256="a" * 64,
    )
    unsigned = load_progress_attestation(_envelope_bytes(payload))
    private, public, signer_id = signing.load_or_create_signer(tmp_path / "keys")
    issuer = {
        "algorithm": "ed25519",
        "public_key": public,
        "signature": signing.sign(private, unsigned.semantic_bytes),
        "signer_id": signer_id,
    }
    raw = _envelope_bytes(payload, issuer=issuer)

    _assert_code("issuer_untrusted", lambda: load_progress_attestation(raw))
    loaded = load_progress_attestation(raw, trusted_issuers={signer_id})
    assert loaded.trust_mode == "configured_issuer_verified"
    assert loaded.issuer_id == signer_id

    forged = dict(issuer)
    forged["signature"] = "00" * 64
    _assert_code(
        "signature_invalid",
        lambda: load_progress_attestation(
            _envelope_bytes(payload, issuer=forged), trusted_issuers={public}
        ),
    )


def test_verify_file_attestation_round_trip_and_state_payload(repo: Path) -> None:
    context = _context(repo)
    (repo / "src" / "feature.txt").write_bytes(b"after working tree\n")
    digest = hashlib.sha256(b"after working tree\n").hexdigest()
    payload = _payload(
        context,
        kind="file",
        commit_sha=context.claim_start_sha,
        file_sha256=digest,
    )
    loaded = load_progress_attestation(_envelope_bytes(payload))

    verified = verify_progress_attestation(
        loaded,
        context,
        project_root=repo,
        now=_NOW + dt.timedelta(seconds=2),
        lease_expires_at=_NOW + dt.timedelta(hours=1),
    )

    state = verified.model_dump()
    assert state["semantic_digest"] == loaded.semantic_digest
    assert state["kind"] == "file"
    assert state["attested_at"] == "2026-08-08T12:00:01Z"
    assert state["trust_mode"] == "claim_owner_self_attested"
    assert state["claim_id"] == "CL-1"
    assert state["generation"] == 2
    assert state["path"] == "src/feature.txt"
    assert state["changed_paths"] == []
    assert state["file_sha256"] == digest
    assert ProgressAttestedPayload.model_validate(state).kind == "file"

    later_payload = dict(payload)
    later_payload["issued_at"] = (
        (_NOW + dt.timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
    )
    later_loaded = load_progress_attestation(_envelope_bytes(later_payload))
    later_verified = verify_progress_attestation(
        later_loaded,
        context,
        project_root=repo,
        now=_NOW + dt.timedelta(seconds=4),
        lease_expires_at=_NOW + dt.timedelta(hours=1),
    )
    assert later_verified.loaded.semantic_digest == loaded.semantic_digest
    assert later_verified.model_dump()["semantic_digest"] == state["semantic_digest"]


def test_verify_commit_attestation_requires_descendant_changed_regular_blob(
    repo: Path,
) -> None:
    context = _context(repo)
    (repo / "src" / "feature.txt").write_bytes(b"after commit\n")
    _git(repo, "add", "--", "src/feature.txt")
    _git(repo, "commit", "-m", "feature")
    commit = _git(repo, "rev-parse", "HEAD")
    digest = hashlib.sha256(b"after commit\n").hexdigest()
    loaded = load_progress_attestation(
        _envelope_bytes(
            _payload(
                context,
                kind="commit",
                commit_sha=commit,
                file_sha256=digest,
            )
        )
    )

    verified = verify_progress_attestation(
        loaded,
        context,
        project_root=repo,
        now=_NOW + dt.timedelta(seconds=2),
    )
    assert verified.state_payload["commit_sha"] == commit
    assert verified.state_payload["changed_paths"] == ["src/feature.txt"]
    assert verified.state_payload["path"] is None
    assert verified.state_payload["kind"] == "commit"
    assert (
        ProgressAttestedPayload.model_validate(verified.model_dump()).kind == "commit"
    )


def test_verifier_refuses_wrong_owner_stale_replay_and_unexpected_path(
    repo: Path,
) -> None:
    context = _context(repo)
    (repo / "src" / "feature.txt").write_bytes(b"after\n")
    digest = hashlib.sha256(b"after\n").hexdigest()
    payload = _payload(
        context,
        kind="file",
        commit_sha=context.claim_start_sha,
        file_sha256=digest,
    )

    wrong_owner = dict(payload)
    wrong_owner["claimed_by"] = "other-agent"
    loaded_wrong_owner = load_progress_attestation(_envelope_bytes(wrong_owner))
    _assert_code(
        "claim_binding_mismatch",
        lambda: verify_progress_attestation(
            loaded_wrong_owner,
            context,
            project_root=repo,
            now=_NOW + dt.timedelta(seconds=2),
        ),
    )

    loaded = load_progress_attestation(_envelope_bytes(payload))
    _assert_code(
        "claim_inactive",
        lambda: verify_progress_attestation(
            loaded,
            context,
            project_root=repo,
            now=_NOW + dt.timedelta(seconds=2),
            claim_status="released",
            released_at=_NOW + dt.timedelta(seconds=1),
        ),
    )

    outside = dict(payload)
    outside["path"] = "src/other.txt"
    outside["prior_sha256"] = None
    loaded_outside = load_progress_attestation(_envelope_bytes(outside))
    _assert_code(
        "path_unexpected",
        lambda: verify_progress_attestation(
            loaded_outside,
            context,
            project_root=repo,
            now=_NOW + dt.timedelta(seconds=2),
        ),
    )


@pytest.mark.skipif(
    os.name == "nt", reason="Windows symlink creation needs host privileges"
)
def test_context_capture_refuses_symlink_escape(tmp_path: Path, repo: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    link = repo / "src" / "link.txt"
    link.symlink_to(outside)

    with pytest.raises(ProgressAttestationError) as caught:
        capture_claim_progress_context(
            repo,
            project_id="P-1",
            claim_id="CL-1",
            claim_generation=1,
            task_id="T007",
            task_snapshot={"id": "T007"},
            prd_id="prd",
            prd_revision=1,
            actor="agent",
            claim_created_at=_NOW,
            expected_paths=["src/link.txt"],
        )
    assert caught.value.code == "path_link"
