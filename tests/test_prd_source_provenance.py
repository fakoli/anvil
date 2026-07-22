"""Revision-bound PRD source identity and exact-byte ingestion gates."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, BinaryIO

import pytest
import typer
from pydantic import ValidationError

import anvil.cli._helpers as helpers_module
import anvil.cli.prd as prd_module
from anvil.cli._helpers import (
    MAX_PRD_SOURCE_BYTES_V1,
    IngestedPrdSource,
    PrdSourceIngestError,
    ingest_prd_source,
    ingest_prd_source_for_id,
    prd_id_from_source_filename,
    prd_source_filename,
    prd_source_path,
    validate_prd_id,
)
from anvil.read_contracts import PrdScopedRefV1
from anvil.state.models import PRD
from anvil.state.payloads import PrdParsedPayload, PrdRevisedPayload


def _available_source_fields(source_bytes: bytes, revision: int) -> dict[str, Any]:
    return {
        "source_text": source_bytes.decode("utf-8"),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_size_bytes": len(source_bytes),
        "source_encoding": "utf-8",
        "source_revision": revision,
        "provenance_state": "available",
        "content_available": True,
    }


@pytest.mark.parametrize(
    ("payload_type", "revision"),
    [(PrdParsedPayload, 1), (PrdRevisedPayload, 3)],
)
def test_payload_available_provenance_binds_exact_bytes_and_revision(
    payload_type: type[PrdParsedPayload | PrdRevisedPayload],
    revision: int,
) -> None:
    source_bytes = "# Project: Crème\r\n\r\nNFD e\u0301 🚀\r\n".encode()
    payload_data: dict[str, Any] = {
        "project_id": "project",
        **_available_source_fields(source_bytes, revision),
    }
    if payload_type is PrdRevisedPayload:
        payload_data["revision"] = revision

    payload = payload_type.model_validate(payload_data)

    assert payload.source_text is not None
    assert payload.source_text.encode("utf-8") == source_bytes
    assert payload.source_sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert payload.source_size_bytes == len(source_bytes)
    assert payload.source_encoding == "utf-8"
    assert payload.source_revision == revision
    assert payload.provenance_state == "available"
    assert payload.content_available is True


@pytest.mark.parametrize("payload_type", [PrdParsedPayload, PrdRevisedPayload])
def test_legacy_payload_model_is_explicitly_unavailable_without_shape_drift(
    payload_type: type[PrdParsedPayload | PrdRevisedPayload],
) -> None:
    payload_data: dict[str, Any] = {"project_id": "legacy"}
    if payload_type is PrdRevisedPayload:
        payload_data["revision"] = 2

    payload = payload_type.model_validate(payload_data)

    assert payload.provenance_state == "legacy_unbound"
    assert payload.content_available is False
    assert payload.source_text is None
    assert payload.source_sha256 is None
    assert payload.source_size_bytes is None
    assert payload.source_encoding is None
    assert payload.source_revision is None
    assert not {
        "source_text",
        "source_sha256",
        "source_size_bytes",
        "source_encoding",
        "source_revision",
        "provenance_state",
        "content_available",
    }.intersection(payload.model_dump(exclude_unset=True))


@pytest.mark.parametrize(
    "override",
    [
        {"source_sha256": None},
        {"source_sha256": "0" * 64},
        {"source_size_bytes": 1},
        {"source_encoding": "utf-16"},
        {"source_revision": 2},
        {"content_available": False},
        {"content_available": 1},
    ],
)
def test_payload_invalid_available_provenance_fails_without_source_echo(
    override: dict[str, Any],
) -> None:
    source_bytes = b"PRIVATE-PROVENANCE-SENTINEL\r\n"
    payload_data = {
        "project_id": "project",
        **_available_source_fields(source_bytes, 1),
        **override,
    }

    with pytest.raises(ValidationError) as refusal:
        PrdParsedPayload.model_validate(payload_data)

    assert "PRIVATE-PROVENANCE-SENTINEL" not in str(refusal.value)


def test_payload_legacy_provenance_cannot_fabricate_metadata() -> None:
    with pytest.raises(ValidationError, match="cannot fabricate"):
        PrdParsedPayload(
            project_id="legacy",
            provenance_state="legacy_unbound",
            content_available=False,
            source_sha256="0" * 64,
        )


def test_payload_invalid_unicode_source_fails_without_source_echo() -> None:
    payload_data = {
        "project_id": "project",
        "source_text": "PRIVATE-SURROGATE-\ud800",
        "source_sha256": "0" * 64,
        "source_size_bytes": 1,
        "source_encoding": "utf-8",
        "source_revision": 1,
        "provenance_state": "available",
        "content_available": True,
    }

    with pytest.raises(ValidationError) as refusal:
        PrdParsedPayload.model_validate(payload_data)

    assert "PRIVATE-SURROGATE" not in str(refusal.value)


def test_projection_model_hides_raw_source_from_generic_dumps() -> None:
    source_bytes = b"# Project: Projection\r\n"
    prd = PRD(
        revision=4,
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_size_bytes=len(source_bytes),
        source_encoding="utf-8",
        source_revision=4,
        provenance_state="available",
        content_available=True,
    )

    generic = prd.model_dump(mode="json")
    python_dump = prd.model_dump()
    assert prd.source_bytes == source_bytes
    assert prd.source_revision == prd.revision
    assert not any(key.startswith("source_") for key in generic)
    assert "provenance_state" not in generic
    assert "content_available" not in generic
    assert not any(key.startswith("source_") for key in python_dump)
    assert "provenance_state" not in python_dump
    assert "content_available" not in python_dump
    assert "source_bytes" not in dict(prd)
    assert "provenance_state" not in dict(prd)
    assert "content_available" not in dict(prd)
    assert repr(source_bytes) not in repr(prd)


def test_projection_provenance_is_immutable_and_updates_revalidate() -> None:
    source_bytes = b"# Project: Immutable\r\n"
    prd = PRD(
        revision=4,
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_size_bytes=len(source_bytes),
        source_encoding="utf-8",
        source_revision=4,
        provenance_state="available",
        content_available=True,
    )

    with pytest.raises(ValidationError):
        prd.source_sha256 = "0" * 64
    assert prd.source_sha256 == hashlib.sha256(source_bytes).hexdigest()

    with pytest.raises(ValidationError):
        prd.validated_copy(source_revision=5)
    assert prd.source_revision == 4


@pytest.mark.parametrize(
    ("payload_field", "invalid_value"),
    [("source_text", b"coerced"), ("source_revision", "1")],
)
def test_payload_provenance_rejects_coercible_types(
    payload_field: str, invalid_value: object
) -> None:
    source_bytes = b"coerced"
    payload = {
        "project_id": "project",
        **_available_source_fields(source_bytes, 1),
        payload_field: invalid_value,
    }

    with pytest.raises(ValidationError):
        PrdParsedPayload.model_validate(payload)


def test_projection_source_bytes_and_revision_reject_coercible_types() -> None:
    with pytest.raises(ValidationError):
        PRD(source_bytes="not-bytes")
    with pytest.raises(ValidationError):
        PRD(revision="1")


@pytest.mark.parametrize(
    "override",
    [
        {"source_sha256": None},
        {"source_sha256": "0" * 64},
        {"source_size_bytes": 1},
        {"source_encoding": None},
        {"source_revision": 5},
        {"content_available": False},
        {"content_available": 1},
    ],
)
def test_projection_model_rejects_inconsistent_available_provenance(
    override: dict[str, Any],
) -> None:
    source_bytes = b"PRIVATE-PROJECTION-SENTINEL\r\n"
    fields: dict[str, Any] = {
        "revision": 4,
        "source_bytes": source_bytes,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_size_bytes": len(source_bytes),
        "source_encoding": "utf-8",
        "source_revision": 4,
        "provenance_state": "available",
        "content_available": True,
        **override,
    }

    with pytest.raises(ValidationError) as refusal:
        PRD.model_validate(fields)

    assert "PRIVATE-PROJECTION-SENTINEL" not in str(refusal.value)


def test_projection_model_rejects_non_utf8_source_without_echo() -> None:
    source_bytes = b"PRIVATE-PROJECTION-SENTINEL-\xff"

    with pytest.raises(ValidationError) as refusal:
        PRD(
            revision=4,
            source_bytes=source_bytes,
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            source_size_bytes=len(source_bytes),
            source_encoding="utf-8",
            source_revision=4,
            provenance_state="available",
            content_available=True,
        )

    assert "PRIVATE-PROJECTION-SENTINEL" not in str(refusal.value)


def test_projection_model_defaults_legacy_provenance_to_unavailable() -> None:
    prd = PRD()
    assert prd.provenance_state == "legacy_unbound"
    assert prd.content_available is False
    assert prd.source_bytes is None
    assert prd.source_sha256 is None
    assert prd.source_size_bytes is None
    assert prd.source_encoding is None
    assert prd.source_revision is None


@pytest.mark.parametrize(
    "source_bytes",
    [
        b"# LF\n\nline\n",
        b"# CRLF\r\n\r\nline\r\n",
        "# NFC\n\né\n".encode(),
        "# NFD\n\ne\u0301\n".encode(),
        "# non-BMP\n\n\U0001f680\n".encode(),
    ],
)
def test_ingest_preserves_exact_utf8_bytes_and_returns_no_path(
    tmp_path: Path,
    source_bytes: bytes,
) -> None:
    source_path = tmp_path / "source.md"
    source_path.write_bytes(source_bytes)

    ingested = ingest_prd_source(source_path)

    assert isinstance(ingested, IngestedPrdSource)
    assert ingested.source_bytes == source_bytes
    assert ingested.markdown.encode("utf-8") == source_bytes
    assert ingested.source_sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert ingested.source_size_bytes == len(source_bytes)
    assert ingested.source_encoding == "utf-8"
    assert "path" not in IngestedPrdSource.__slots__


def test_ingest_limit_uses_one_bounded_limit_plus_one_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source.md"
    source_path.write_bytes(b"12345678")
    read_sizes: list[int] = []
    real_fdopen = os.fdopen

    class TrackingStream:
        def __init__(self, descriptor: int, mode: str, *, closefd: bool) -> None:
            self.stream: BinaryIO = real_fdopen(descriptor, mode, closefd=closefd)

        def __enter__(self) -> TrackingStream:
            return self

        def __exit__(self, *args: object) -> None:
            self.stream.close()

        def read(self, size: int) -> bytes:
            read_sizes.append(size)
            return self.stream.read(size)

        def fileno(self) -> int:
            return self.stream.fileno()

    def tracking_fdopen(
        descriptor: int,
        mode: str,
        *,
        closefd: bool,
    ) -> TrackingStream:
        return TrackingStream(descriptor, mode, closefd=closefd)

    monkeypatch.setattr(helpers_module.os, "fdopen", tracking_fdopen)

    ingested = ingest_prd_source(source_path, max_bytes=8)

    assert ingested.source_bytes == b"12345678"
    assert read_sizes == [9]


def test_ingest_limit_refuses_exact_overrun_before_utf8_decode(tmp_path: Path) -> None:
    source_path = tmp_path / "over-limit.md"
    source_path.write_bytes((b"x" * MAX_PRD_SOURCE_BYTES_V1) + b"\xff")

    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source(source_path)

    assert refusal.value.code == "source_limit_exceeded"
    assert str(source_path) not in str(refusal.value)


def test_ingest_limit_accepts_n_and_refuses_n_plus_one(tmp_path: Path) -> None:
    source_path = tmp_path / "boundary.md"
    source_path.write_bytes(b"12345678")
    assert ingest_prd_source(source_path, max_bytes=8).source_size_bytes == 8

    source_path.write_bytes(b"123456789")
    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source(source_path, max_bytes=8)
    assert refusal.value.code == "source_limit_exceeded"


@pytest.mark.parametrize("max_bytes", [0, -1, True, MAX_PRD_SOURCE_BYTES_V1 + 1])
def test_ingest_limit_must_be_within_the_fixed_provider_ceiling(
    tmp_path: Path,
    max_bytes: object,
) -> None:
    source_path = tmp_path / "source.md"
    source_path.write_bytes(b"safe")

    with pytest.raises(ValueError, match="Version 1 ceiling"):
        ingest_prd_source(source_path, max_bytes=max_bytes)  # type: ignore[arg-type]


def test_ingest_utf8_refusal_is_typed_bounded_and_path_safe(tmp_path: Path) -> None:
    source_path = tmp_path / "invalid-utf8.md"
    source_path.write_bytes(b"valid-prefix\xffsecret-suffix")

    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source(source_path)

    assert refusal.value.code == "source_invalid_utf8"
    diagnostic = str(refusal.value)
    assert "valid-prefix" not in diagnostic
    assert "secret-suffix" not in diagnostic
    assert str(source_path) not in diagnostic


def test_ingest_utf8_preserves_nul_bytes_without_an_unstated_restriction(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "nul.md"
    source_bytes = b"before\x00after"
    source_path.write_bytes(source_bytes)
    ingested = ingest_prd_source(source_path)
    assert ingested.source_bytes == source_bytes
    assert ingested.markdown.encode("utf-8") == source_bytes


@pytest.mark.parametrize(
    "prd_id",
    [
        "../escape",
        "..",
        ".",
        "/absolute",
        r"C:\drive",
        r"\\server\share",
        "with/slash",
        r"with\backslash",
        "with:colon",
        "with\x00nul",
        " leading",
        "trailing ",
        "-leading",
        "trailing-",
    ],
)
def test_identity_refuses_path_shaped_prd_ids_before_resolution(prd_id: str) -> None:
    with pytest.raises(PrdSourceIngestError) as refusal:
        validate_prd_id(prd_id)
    assert refusal.value.code == "invalid_prd_id"
    assert prd_id not in str(refusal.value)


@pytest.mark.parametrize(
    "prd_id",
    [
        "default",
        "prd",
        "v0.2",
        "release_2026-07",
        "A",
        "a" * 128,
        "CON",
        "nul.release",
        "Com1",
        "LPT9.notes",
    ],
)
def test_identity_accepts_closed_canonical_prd_ids(prd_id: str) -> None:
    validated = validate_prd_id(prd_id)
    assert validated == prd_id
    assert type(validated) is str
    assert PrdScopedRefV1(prd_id=prd_id).prd_id == prd_id


@pytest.mark.parametrize("prd_id", ["CON", "nul.release", "Com1", "LPT9.notes"])
def test_identity_maps_windows_aliases_reversibly_without_narrowing_v1(
    tmp_path: Path,
    prd_id: str,
) -> None:
    filename = prd_source_filename(prd_id)
    assert filename.startswith("_anvil-prd-")
    assert prd_id_from_source_filename(filename) == prd_id
    assert prd_source_path(tmp_path / ".anvil", prd_id).name == filename
    assert PrdScopedRefV1(prd_id=prd_id).prd_id == prd_id


def test_identity_long_windows_alias_mapping_fits_component_ceiling() -> None:
    prd_id = "CON." + ("a" * 124)
    filename = prd_source_filename(prd_id)
    assert len(prd_id.encode("ascii")) == 128
    assert len(filename) <= 255
    assert prd_id_from_source_filename(filename) == prd_id


def test_identity_case_distinct_ids_have_distinct_portable_filenames() -> None:
    uppercase_filename = prd_source_filename("A")
    lowercase_filename = prd_source_filename("a")
    assert uppercase_filename.casefold() != lowercase_filename.casefold()
    assert prd_id_from_source_filename(uppercase_filename) == "A"
    assert prd_id_from_source_filename(lowercase_filename) == "a"


def test_identity_legacy_uppercase_source_remains_readable(tmp_path: Path) -> None:
    state_dir = tmp_path / ".anvil"
    legacy_source = state_dir / "prds" / "Release.md"
    legacy_source.parent.mkdir(parents=True)
    legacy_source.write_bytes(b"# Legacy\r\n")

    if os.name == "nt":
        with pytest.raises(PrdSourceIngestError) as refusal:
            ingest_prd_source_for_id(state_dir, "Release")
        assert refusal.value.code == "legacy_source_migration_required"
    else:
        ingested = ingest_prd_source_for_id(state_dir, "Release")
        assert ingested.source_bytes == b"# Legacy\r\n"


def test_identity_portable_and_legacy_sources_refuse_as_ambiguous(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".anvil"
    portable_source = prd_source_path(state_dir, "Release")
    portable_source.parent.mkdir(parents=True)
    portable_source.write_bytes(b"portable")
    (portable_source.parent / "Release.md").write_bytes(b"legacy")

    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source_for_id(state_dir, "Release")

    assert refusal.value.code == "source_ambiguous"


@pytest.mark.skipif(os.name == "nt", reason="Windows legacy sources require migration")
def test_identity_legacy_selection_rechecks_late_portable_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".anvil"
    legacy_source = state_dir / "prds" / "Release.md"
    legacy_source.parent.mkdir(parents=True)
    legacy_source.write_bytes(b"legacy")
    portable_source = prd_source_path(state_dir, "Release")
    real_lexists = os.path.lexists
    first_portable_probe = True

    def racing_lexists(path: os.PathLike[str]) -> bool:
        nonlocal first_portable_probe
        if Path(path) == portable_source and first_portable_probe:
            first_portable_probe = False
            portable_source.write_bytes(b"portable")
            return False
        return real_lexists(path)

    monkeypatch.setattr(helpers_module.os.path, "lexists", racing_lexists)
    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source_for_id(state_dir, "Release")

    assert refusal.value.code == "source_ambiguous"


@pytest.mark.skipif(os.name != "nt", reason="requires case-insensitive Windows paths")
def test_identity_windows_legacy_case_aliases_fail_closed(tmp_path: Path) -> None:
    state_dir = tmp_path / ".anvil"
    legacy_source = state_dir / "prds" / "A.md"
    legacy_source.parent.mkdir(parents=True)
    legacy_source.write_bytes(b"legacy-uppercase")

    with pytest.raises(PrdSourceIngestError) as uppercase_refusal:
        ingest_prd_source_for_id(state_dir, "A")
    with pytest.raises(PrdSourceIngestError) as lowercase_refusal:
        ingest_prd_source_for_id(state_dir, "a")

    assert uppercase_refusal.value.code == "legacy_source_migration_required"
    assert lowercase_refusal.value.code == "source_case_alias"


@pytest.mark.skipif(os.name != "nt", reason="requires case-insensitive Windows paths")
def test_identity_windows_case_alias_swap_before_open_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".anvil"
    source_root = state_dir / "prds"
    source_root.mkdir(parents=True)
    real_ingest = helpers_module.ingest_prd_source

    def racing_ingest(source_path: Path, **kwargs: Any) -> IngestedPrdSource:
        (source_root / "A.md").write_bytes(b"uppercase-alias")
        return real_ingest(source_path, **kwargs)

    monkeypatch.setattr(helpers_module, "ingest_prd_source", racing_ingest)
    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source_for_id(state_dir, "a")

    assert refusal.value.code == "source_case_alias"


def test_identity_encoded_windows_alias_source_ingests_normally(tmp_path: Path) -> None:
    state_dir = tmp_path / ".anvil"
    source_path = prd_source_path(state_dir, "CON")
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"# Safe alias\r\n")

    ingested = ingest_prd_source_for_id(state_dir, "CON")

    assert ingested.source_bytes == b"# Safe alias\r\n"
    assert prd_id_from_source_filename(source_path.name) == "CON"


def test_identity_refuses_named_source_symlink_escape(tmp_path: Path) -> None:
    state_dir = tmp_path / ".anvil"
    prds_dir = state_dir / "prds"
    prds_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside")
    link = prds_dir / "release.md"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc.__class__.__name__}")

    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source_for_id(state_dir, "release")

    assert refusal.value.code == "source_outside_prd_directory"
    assert str(outside) not in str(refusal.value)


def test_identity_refuses_named_source_directory_symlink_escape(tmp_path: Path) -> None:
    state_dir = tmp_path / ".anvil"
    state_dir.mkdir()
    outside = tmp_path / "outside-prds"
    outside.mkdir()
    (outside / "release.md").write_bytes(b"outside")
    source_root = state_dir / "prds"
    try:
        source_root.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc.__class__.__name__}")

    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source_for_id(state_dir, "release")

    assert refusal.value.code == "source_outside_prd_directory"
    assert str(outside) not in str(refusal.value)


def test_ingest_refuses_file_swap_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source.md"
    source_path.write_bytes(b"contained")
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside-secret")
    real_open = os.open
    phases: list[str] = []

    def swapped_open(path: os.PathLike[str], flags: int) -> int:
        phases.append("open-outside")
        return real_open(outside, flags)

    def fail_read(*args: object, **kwargs: object) -> None:
        phases.append("read")
        raise AssertionError("mismatched handle must refuse before reading")

    monkeypatch.setattr(helpers_module.os, "open", swapped_open)
    monkeypatch.setattr(helpers_module.os, "fdopen", fail_read)
    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source(source_path)

    assert refusal.value.code == "source_changed"
    assert phases == ["open-outside"]
    assert "outside-secret" not in str(refusal.value)


def test_ingest_refuses_file_swap_to_outside_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source.md"
    source_path.write_bytes(b"contained")
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside-secret")
    real_open = os.open

    try:
        probe = tmp_path / "symlink-probe.md"
        probe.symlink_to(outside)
        probe.unlink()
    except OSError as exc:
        pytest.skip(f"file symlink unavailable: {exc.__class__.__name__}")

    def swapped_open(path: os.PathLike[str], flags: int) -> int:
        source_path.unlink()
        source_path.symlink_to(outside)
        return real_open(path, flags)

    monkeypatch.setattr(helpers_module.os, "open", swapped_open)
    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source(source_path)

    assert refusal.value.code in {
        "source_changed",
        "source_outside_prd_directory",
    }
    assert "outside-secret" not in str(refusal.value)


def test_identity_refuses_parent_swap_to_outside_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".anvil"
    source_root = state_dir / "prds"
    source_root.mkdir(parents=True)
    source_path = source_root / "release.md"
    source_path.write_bytes(b"contained")
    outside_root = tmp_path / "outside-prds"
    outside_root.mkdir()
    (outside_root / "release.md").write_bytes(b"outside-secret")
    backup_root = state_dir / "prds-original"
    real_open = os.open

    try:
        probe = tmp_path / "symlink-probe"
        probe.symlink_to(outside_root, target_is_directory=True)
        probe.unlink()
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc.__class__.__name__}")

    def swapped_parent_open(
        path: os.PathLike[str],
        flags: int,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(path).name != "release.md":
            if dir_fd is None:
                return real_open(path, flags)
            return real_open(path, flags, dir_fd=dir_fd)
        source_root.rename(backup_root)
        source_root.symlink_to(outside_root, target_is_directory=True)
        if dir_fd is None:
            return real_open(path, flags)
        return real_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(helpers_module.os, "open", swapped_parent_open)
    try:
        ingested = ingest_prd_source_for_id(state_dir, "release")
    except PrdSourceIngestError as refusal:
        assert refusal.code == "source_outside_prd_directory"
        assert "outside-secret" not in str(refusal)
    else:
        assert ingested.source_bytes == b"contained"


def test_identity_parent_swap_back_cannot_launder_outside_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".anvil"
    source_root = state_dir / "prds"
    source_root.mkdir(parents=True)
    (source_root / "release.md").write_bytes(b"contained")
    outside_root = tmp_path / "outside-prds"
    outside_root.mkdir()
    outside_source = outside_root / "release.md"
    outside_source.write_bytes(b"outside-secret")
    backup_root = state_dir / "prds-original"
    real_open = os.open

    try:
        probe = tmp_path / "symlink-probe"
        probe.symlink_to(outside_root, target_is_directory=True)
        probe.unlink()
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc.__class__.__name__}")

    def swapped_then_restored_open(
        path: os.PathLike[str],
        flags: int,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(path).name != "release.md":
            if dir_fd is None:
                return real_open(path, flags)
            return real_open(path, flags, dir_fd=dir_fd)
        source_root.rename(backup_root)
        source_root.symlink_to(outside_root, target_is_directory=True)
        descriptor = real_open(outside_source, flags)
        source_root.unlink()
        backup_root.rename(source_root)
        return descriptor

    monkeypatch.setattr(helpers_module.os, "open", swapped_then_restored_open)
    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source_for_id(state_dir, "release")

    assert refusal.value.code in {"source_changed", "source_outside_prd_directory"}
    assert "outside-secret" not in str(refusal.value)


def test_ingest_same_size_mtime_restored_mutation_never_passes_as_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source.md"
    original = b"original"
    replacement = b"replaced"
    source_path.write_bytes(original)
    initial_stat = source_path.stat()
    real_fdopen = os.fdopen
    mutation_blocked: list[bool] = []

    class MutatingStream:
        def __init__(self, descriptor: int, mode: str, *, closefd: bool) -> None:
            self.stream: BinaryIO = real_fdopen(descriptor, mode, closefd=closefd)

        def __enter__(self) -> MutatingStream:
            return self

        def __exit__(self, *args: object) -> None:
            self.stream.close()

        def read(self, size: int) -> bytes:
            source_bytes = self.stream.read(size)
            try:
                source_path.write_bytes(replacement)
                os.utime(
                    source_path,
                    ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns),
                )
            except OSError:
                mutation_blocked.append(True)
            return source_bytes

        def fileno(self) -> int:
            return self.stream.fileno()

    monkeypatch.setattr(
        helpers_module.os,
        "fdopen",
        lambda descriptor, mode, *, closefd: MutatingStream(
            descriptor,
            mode,
            closefd=closefd,
        ),
    )

    try:
        ingested = ingest_prd_source(source_path)
    except PrdSourceIngestError as refusal:
        assert refusal.code == "source_changed"
    else:
        assert mutation_blocked == [True]
        assert ingested.source_bytes == original


@pytest.mark.skipif(os.name != "nt", reason="Windows shared-lock behavior")
def test_ingest_windows_shared_lock_allows_parallel_readers(tmp_path: Path) -> None:
    source_path = tmp_path / "source.md"
    source_path.write_bytes(b"parallel readers")
    descriptor, _opened = helpers_module._open_verified_prd_source(
        source_path,
        containment_root=None,
        required_parent=None,
    )
    try:
        helpers_module._lock_prd_source_descriptor(
            descriptor,
            byte_count=MAX_PRD_SOURCE_BYTES_V1 + 1,
        )
        ingested = ingest_prd_source(source_path)
    finally:
        os.close(descriptor)

    assert ingested.source_bytes == b"parallel readers"


def test_ingest_refuses_fifo_before_open_or_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("POSIX FIFO creation unavailable")
    source_path = tmp_path / "source.md"
    os.mkfifo(source_path)
    phases: list[str] = []

    def fail_open(*args: object, **kwargs: object) -> int:
        phases.append("open")
        raise AssertionError("FIFO must refuse before potentially blocking open")

    monkeypatch.setattr(helpers_module.os, "open", fail_open)
    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source(source_path)

    assert refusal.value.code == "source_not_regular"
    assert phases == []


@pytest.mark.parametrize(
    ("source_bytes", "expected_code"),
    [
        (b"invalid\xff", "source_invalid_utf8"),
        (
            (b"x" * MAX_PRD_SOURCE_BYTES_V1) + b"\xff",
            "source_limit_exceeded",
        ),
    ],
    ids=["invalid-utf8", "source-limit"],
)
def test_prd_parse_utf8_and_limit_refuse_before_backend_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_bytes: bytes,
    expected_code: str,
) -> None:
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "local")
    (tmp_path / ".anvil").mkdir()
    source_path = tmp_path / "source.md"
    source_path.write_bytes(source_bytes)
    phases: list[str] = []

    def fail_backend(_state_dir: Path) -> None:
        phases.append("backend")
        raise AssertionError("backend must not open before source validation")

    monkeypatch.setattr(prd_module, "_open_backend", fail_backend)
    with pytest.raises(typer.Exit):
        prd_module.prd_parse(file=source_path, prd="default", cwd=tmp_path)

    assert phases == []
    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source(source_path)
    assert refusal.value.code == expected_code


@pytest.mark.parametrize("invalid_prd_id", ["../escape", ""])
def test_prd_parse_identity_refuses_before_file_access_or_backend_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_prd_id: str,
) -> None:
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "local")
    (tmp_path / ".anvil").mkdir()
    phases: list[str] = []

    def fail_ingest(_source_path: Path) -> None:
        phases.append("ingest")
        raise AssertionError("invalid PRD id must refuse before file access")

    def fail_backend(_state_dir: Path) -> None:
        phases.append("backend")
        raise AssertionError("invalid PRD id must refuse before backend open")

    monkeypatch.setattr(prd_module, "ingest_prd_source", fail_ingest)
    monkeypatch.setattr(prd_module, "_open_backend", fail_backend)
    with pytest.raises(typer.Exit):
        prd_module.prd_parse(
            file=tmp_path / "arbitrary.md",
            prd=invalid_prd_id,
            cwd=tmp_path,
        )

    assert phases == []


def test_prd_parse_relative_file_uses_explicit_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "local")
    (tmp_path / ".anvil").mkdir()
    observed: list[Path] = []

    def capture_source(source_path: Path) -> IngestedPrdSource:
        observed.append(source_path)
        raise PrdSourceIngestError("source_not_found", "PRD source not found")

    monkeypatch.setattr(prd_module, "ingest_prd_source", capture_source)
    with pytest.raises(typer.Exit):
        prd_module.prd_parse(file=Path("relative.md"), prd=None, cwd=tmp_path)

    assert observed == [tmp_path.resolve() / "relative.md"]
