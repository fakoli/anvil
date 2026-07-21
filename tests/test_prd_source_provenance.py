"""Revision-bound PRD source identity and exact-byte ingestion gates."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, mock_open

import pytest
import typer

import anvil.cli.prd as prd_module
from anvil.cli._helpers import (
    MAX_PRD_SOURCE_BYTES_V1,
    IngestedPrdSource,
    PrdSourceIngestError,
    ingest_prd_source,
    prd_source_path,
    validate_prd_id,
)


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


def test_ingest_limit_uses_one_bounded_limit_plus_one_probe() -> None:
    source_path = MagicMock(spec=Path)
    opener = mock_open(read_data=b"12345678")
    source_path.open = opener

    ingested = ingest_prd_source(source_path, max_bytes=8)

    assert ingested.source_bytes == b"12345678"
    opener.assert_called_once_with("rb")
    opener().read.assert_called_once_with(9)


def test_ingest_limit_refuses_exact_overrun_before_utf8_decode(tmp_path: Path) -> None:
    source_path = tmp_path / "over-limit.md"
    source_path.write_bytes((b"x" * MAX_PRD_SOURCE_BYTES_V1) + b"\xff")

    with pytest.raises(PrdSourceIngestError) as refusal:
        ingest_prd_source(source_path)

    assert refusal.value.code == "source_limit_exceeded"
    assert str(source_path) not in str(refusal.value)


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
        "CON",
        "nul.release",
        "Com1",
        "LPT9.notes",
    ],
)
def test_identity_refuses_path_shaped_prd_ids_before_resolution(prd_id: str) -> None:
    with pytest.raises(PrdSourceIngestError) as refusal:
        validate_prd_id(prd_id)
    assert refusal.value.code == "invalid_prd_id"
    assert prd_id not in str(refusal.value)


@pytest.mark.parametrize(
    "prd_id",
    ["default", "prd", "v0.2", "release_2026-07", "A", "a" * 128],
)
def test_identity_accepts_closed_canonical_prd_ids(prd_id: str) -> None:
    validated = validate_prd_id(prd_id)
    assert validated == prd_id
    assert type(validated) is str


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
        prd_source_path(state_dir, "release")

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
        prd_source_path(state_dir, "release")

    assert refusal.value.code == "source_outside_prd_directory"
    assert str(outside) not in str(refusal.value)


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


def test_prd_parse_identity_refuses_before_file_access_or_backend_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            prd="../escape",
            cwd=tmp_path,
        )

    assert phases == []
