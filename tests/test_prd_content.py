"""Public contract tests for ``anvil prd show`` / PRD content v1."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.prd_content import PRD_CONTENT_DIGEST_DOMAIN, PrdContentRefusal, read_prd_content
from anvil.state.hashing import canonical_json_bytes
from anvil.state.schema import SCHEMA_VERSION
from anvil.state.sqlite import SqliteBackend

runner = CliRunner()

SOURCE = (
    b"# Project: exact \xf0\x9f\xa7\xaa\r\n"
    b"\r\n"
    b"## Summary\r\n"
    b"\r\n"
    b"Cafe\xcc\x81 and control text `${not-an-option}`.\r\n"
    b"\r\n"
    b"```markdown\r\n"
    b"## Hidden\r\n"
    b"```\r\n"
    b"\r\n"
    b"## Goals\r\n"
    b"\r\n"
    b"- Preserve exact bytes.\r\n"
    b"\r\n"
    b"### Scope / ~ v1\r\n"
    b"\r\n"
    b"Contained.\r\n"
    b"\r\n"
    b"#### Primary\r\n"
    b"\r\n"
    b"Ship.\r\n"
    b"\r\n"
    b"## Requirements\r\n"
    b"\r\n"
    b"- R001: Return persisted content.\r\n"
)


def _invoke(project: Path, args: list[str]):  # type: ignore[no-untyped-def]
    original = os.getcwd()
    os.chdir(project)
    try:
        return runner.invoke(app, args, catch_exceptions=False)
    finally:
        os.chdir(original)


def _project(tmp_path: Path, source: bytes = SOURCE) -> Path:
    initialized = _invoke(tmp_path, ["init", "--name", "PRD content test"])
    assert initialized.exit_code == 0, initialized.output
    (tmp_path / ".anvil" / "prd.md").write_bytes(source)
    parsed = _invoke(tmp_path, ["prd", "parse", "--json"])
    assert parsed.exit_code == 0, parsed.output
    return tmp_path / ".anvil"


def _show(project: Path, *args: str):  # type: ignore[no-untyped-def]
    return _invoke(project, ["prd", "show", "default", "--json", *args])


def _payload(result) -> dict:  # type: ignore[no-untyped-def,type-arg]
    return json.loads(result.stdout)


def _manifest(state_dir: Path) -> tuple[bytes, bytes]:
    return (
        (state_dir / "state.db").read_bytes(),
        (state_dir / "events.jsonl").read_bytes(),
    )


def _digest(source_digest: str, selector: dict, returned: bytes) -> str:  # type: ignore[type-arg]
    return hashlib.sha256(
        PRD_CONTENT_DIGEST_DOMAIN
        + source_digest.encode("ascii")
        + b"\0"
        + canonical_json_bytes(selector)
        + b"\0"
        + returned
    ).hexdigest()


def test_prd_show_full_preserves_exact_persisted_bytes_and_digest(
    tmp_path: Path,
) -> None:
    state_dir = _project(tmp_path)
    before = _manifest(state_dir)

    result = _show(tmp_path)

    assert result.exit_code == 0, result.output
    assert len(result.stdout.splitlines()) == 1
    envelope = _payload(result)
    assert envelope["ok"] is True
    assert envelope["command"] == "prd show"
    data = envelope["data"]
    source_digest = hashlib.sha256(SOURCE).hexdigest()
    assert data == {
        "operation_id": "state.prd.content",
        "operation_version": 1,
        "schema_id": "anvil.state.prd-content.v1",
        "prd_ref": {"prd_id": "default"},
        "prd_revision": 1,
        "digest_algorithm": "sha256",
        "source_digest": source_digest,
        "content_digest": _digest(
            source_digest, {"kind": "full", "paths": []}, SOURCE
        ),
        "source_size_bytes": len(SOURCE),
        "returned_size_bytes": len(SOURCE),
        "encoding": "utf-8",
        "selected_sections": [],
        "applied_limit_bytes": 2_097_152,
        "truncated": False,
        "content": SOURCE.decode("utf-8"),
    }
    assert data["content"].encode("utf-8") == SOURCE
    assert _manifest(state_dir) == before


def test_prd_show_named_partition_tracks_exact_current_revision(tmp_path: Path) -> None:
    initialized = _invoke(tmp_path, ["init", "--name", "Named PRD content"])
    assert initialized.exit_code == 0, initialized.output
    source_path = tmp_path / "private-input.md"
    source_path.write_bytes(SOURCE)
    first = _invoke(
        tmp_path,
        ["prd", "parse", "--file", str(source_path), "--prd", "v0.2", "--json"],
    )
    assert first.exit_code == 0, first.output
    revised_source = SOURCE.replace(b"Ship.\r\n", b"Ship revision two.\r\n")
    source_path.write_bytes(revised_source)
    revised = _invoke(
        tmp_path,
        ["prd", "parse", "--file", str(source_path), "--prd", "v0.2", "--json"],
    )
    assert revised.exit_code == 0, revised.output

    result = _invoke(tmp_path, ["prd", "show", "v0.2", "--json"])

    assert result.exit_code == 0, result.output
    data = _payload(result)["data"]
    assert data["prd_ref"] == {"prd_id": "v0.2"}
    assert data["prd_revision"] == 2
    assert data["source_digest"] == hashlib.sha256(revised_source).hexdigest()
    assert data["content"].encode("utf-8") == revised_source


def test_prd_content_digest_has_independent_full_and_selected_vectors() -> None:
    source = (
        b"# Project: X\r\n\r\n## Summary\r\ncaf\xc3\xa9\r\n"
        b"## Goals\r\n- ship\r\n"
    )
    source_digest = hashlib.sha256(source).hexdigest()
    selected = b"## Summary\r\ncaf\xc3\xa9\r\n"
    # Literals freeze the wire framing independently of read_prd_content.
    assert _digest(source_digest, {"kind": "full", "paths": []}, source) == (
        "f5e22d65e4df03b1f3a935f3aa049831d0399b87d04196dbacc48633354af72d"
    )
    assert _digest(
        source_digest,
        {"kind": "sections", "paths": ["Summary"]},
        selected,
    ) == "c7418b66a23c89794c79a02c9f325f100ddc0534659d666297afa26a7a60a99c"


def test_prd_show_sections_are_fence_aware_escaped_and_in_source_order(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    result = _show(
        tmp_path,
        "--section",
        "Goals/Scope ~1 ~0 v1/Primary",
        "--section",
        "Summary",
    )
    assert result.exit_code == 0, result.output
    data = _payload(result)["data"]
    assert data["selected_sections"] == ["Summary", "Goals/Scope ~1 ~0 v1/Primary"]
    expected = (
        b"## Summary\r\n\r\nCafe\xcc\x81 and control text `${not-an-option}`.\r\n\r\n"
        b"```markdown\r\n## Hidden\r\n```\r\n\r\n"
        b"#### Primary\r\n\r\nShip.\r\n\r\n"
    )
    assert data["content"].encode("utf-8") == expected
    assert "## Hidden" in data["content"]


@pytest.mark.parametrize(
    "sections",
    [
        ["Unknown"],
        ["Summary", "Summary"],
        ["Goals/Scope ~1 ~0 v1", "Goals/Scope ~1 ~0 v1/Primary"],
        ["Hidden"],
        ["Goals ~2 Scope"],
        ["/Summary"],
    ],
)
def test_prd_show_rejects_invalid_ambiguous_or_overlapping_sections_atomically(
    tmp_path: Path, sections: list[str]
) -> None:
    state_dir = _project(tmp_path)
    before = _manifest(state_dir)
    args = [item for section in sections for item in ("--section", section)]
    result = _show(tmp_path, *args)
    envelope = _payload(result)
    assert result.exit_code == 1
    assert envelope["error"]["code"] == "invalid_section"
    assert "content" not in envelope["error"]
    assert envelope["error"]["truncated"] is False
    assert _manifest(state_dir) == before


def test_prd_show_duplicate_source_heading_is_ambiguous(tmp_path: Path) -> None:
    source = SOURCE + b"\r\n## Summary\r\n\r\nDuplicate.\r\n"
    _project(tmp_path, source)
    result = _show(tmp_path, "--section", "Summary")
    assert result.exit_code == 1
    assert _payload(result)["error"]["code"] == "invalid_section"


def test_prd_show_expected_digest_and_lowered_limit_are_fail_closed(
    tmp_path: Path,
) -> None:
    state_dir = _project(tmp_path)
    before = _manifest(state_dir)
    stale = _show(tmp_path, "--expected-digest", "0" * 64)
    assert stale.exit_code == 1
    assert _payload(stale)["error"]["code"] == "stale_digest"

    limited = _show(tmp_path, "--limit", "12")
    refusal = _payload(limited)
    assert limited.exit_code == 1
    assert refusal["error"]["code"] == "limit_exceeded"
    assert refusal["error"]["actual"] == len(SOURCE)
    assert refusal["error"]["limit"] == 12
    assert refusal["error"]["truncated"] is False
    assert "content" not in refusal["error"]
    assert SOURCE[:12].decode("utf-8", errors="ignore") not in limited.stdout
    assert _manifest(state_dir) == before


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["bad/id", "--json"], "invalid_identifier"),
        (["default", "--json", "--expected-digest", "ABC"], "invalid_request"),
        (["default", "--json", "--limit", "1.5"], "invalid_request"),
        (["default", "--json", "--limit", "999999999999999"], "invalid_request"),
        (["default", "--json", "--section", "bad~2escape"], "invalid_section"),
    ],
)
def test_prd_show_validates_request_before_state_lookup(
    tmp_path: Path, args: list[str], code: str
) -> None:
    result = _invoke(tmp_path, ["prd", "show", *args])
    assert result.exit_code == 1
    assert len(result.stdout.splitlines()) == 1
    assert _payload(result)["error"]["code"] == code
    assert str(tmp_path) not in result.stdout


def test_prd_show_rejects_non_json_with_one_json_document(tmp_path: Path) -> None:
    result = _invoke(tmp_path, ["prd", "show", "default"])
    assert result.exit_code == 1
    assert len(result.stdout.splitlines()) == 1
    assert _payload(result)["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        (
            {
                "source_bytes": sqlite3.Binary(b"\xff"),
                "source_sha256": hashlib.sha256(b"\xff").hexdigest(),
                "source_size_bytes": 1,
            },
            "invalid_utf8",
        ),
        ({"source_sha256": "0" * 64}, "source_drift"),
        (
            {
                "source_bytes": None,
                "source_sha256": None,
                "source_size_bytes": None,
                "source_encoding": None,
                "source_revision": None,
                "provenance_state": "legacy_unbound",
                "content_available": 0,
            },
            "content_unavailable",
        ),
    ],
)
def test_prd_show_corrupt_or_legacy_bindings_fail_closed(
    tmp_path: Path, updates: dict[str, object], code: str
) -> None:
    state_dir = _project(tmp_path)
    connection = sqlite3.connect(state_dir / "state.db")
    try:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        connection.execute(
            f"UPDATE prds SET {assignments} WHERE id = 'default'",  # noqa: S608
            tuple(updates.values()),
        )
        connection.commit()
    finally:
        connection.close()
    before = _manifest(state_dir)
    result = _show(tmp_path)
    assert result.exit_code == 1
    assert _payload(result)["error"]["code"] == code
    assert SOURCE[:16].decode("utf-8") not in result.stdout
    assert _manifest(state_dir) == before


def test_prd_show_refuses_diverged_projection_without_healing(tmp_path: Path) -> None:
    state_dir = _project(tmp_path)
    connection = sqlite3.connect(state_dir / "state.db")
    try:
        connection.execute("DELETE FROM events WHERE rowid = (SELECT MAX(rowid) FROM events)")
        connection.commit()
    finally:
        connection.close()
    before = _manifest(state_dir)
    result = _show(tmp_path)
    assert result.exit_code == 1
    assert _payload(result)["error"]["code"] == "projection_not_converged"
    assert _manifest(state_dir) == before


@pytest.mark.parametrize("damage", ["torn", "duplicate_key", "actor_drift"])
def test_prd_show_strictly_refuses_malformed_or_diverged_event_envelopes(
    tmp_path: Path,
    damage: str,
) -> None:
    state_dir = _project(tmp_path)
    events_path = state_dir / "events.jsonl"
    original = events_path.read_bytes()
    lines = original.splitlines(keepends=True)
    assert lines and lines[0].endswith(b"\n")
    if damage == "torn":
        changed = original + b"{"
    else:
        document = json.loads(lines[0])
        actor_field = json.dumps(
            {"actor": document["actor"]}, separators=(",", ":")
        )[1:-1].encode("utf-8")
        if damage == "duplicate_key":
            lines[0] = lines[0].replace(
                actor_field,
                actor_field + b"," + actor_field,
                1,
            )
        else:
            replacement = json.dumps(
                {"actor": "diverged-reader"}, separators=(",", ":")
            )[1:-1].encode("utf-8")
            lines[0] = lines[0].replace(actor_field, replacement, 1)
        changed = b"".join(lines)
    events_path.write_bytes(changed)
    before = _manifest(state_dir)

    with pytest.raises(PrdContentRefusal) as caught:
        read_prd_content(state_dir, "default")

    assert caught.value.error.code.value == "projection_not_converged"
    assert _manifest(state_dir) == before


def test_prd_show_bounds_one_oversized_event_record_before_json_decode(
    tmp_path: Path,
) -> None:
    state_dir = _project(tmp_path)
    events_path = state_dir / "events.jsonl"
    events_path.write_bytes(events_path.read_bytes() + b" " * (16_777_216 + 2) + b"\n")
    before = _manifest(state_dir)

    with pytest.raises(PrdContentRefusal) as caught:
        read_prd_content(state_dir, "default")

    assert caught.value.error.code.value == "projection_not_converged"
    assert _manifest(state_dir) == before


def test_prd_show_waits_for_log_first_writer_and_returns_complete_post_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = _project(tmp_path)
    revised_source = SOURCE + b"\r\nRevision two.\r\n"
    (state_dir / "prd.md").write_bytes(revised_source)
    log_appended = threading.Event()
    allow_commit = threading.Event()
    writer_done = threading.Event()
    reader_done = threading.Event()
    writer_results: list[object] = []
    reader_results: list[object] = []
    original_insert = SqliteBackend._insert_event_row

    def paused_insert(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        log_appended.set()
        assert allow_commit.wait(5)
        return original_insert(self, *args, **kwargs)

    monkeypatch.setattr(SqliteBackend, "_insert_event_row", paused_insert)

    def writer() -> None:
        try:
            writer_results.append(_invoke(tmp_path, ["prd", "parse", "--json"]))
        finally:
            writer_done.set()

    def reader() -> None:
        try:
            reader_results.append(read_prd_content(state_dir, "default"))
        finally:
            reader_done.set()

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert log_appended.wait(5)
    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    assert not reader_done.wait(0.1)
    allow_commit.set()
    assert writer_done.wait(5)
    assert reader_done.wait(5)
    writer_thread.join()
    reader_thread.join()

    writer_result = writer_results[0]
    assert writer_result.exit_code == 0  # type: ignore[attr-defined]
    response = reader_results[0]
    assert response.prd_revision == 2  # type: ignore[attr-defined]
    assert response.content.encode("utf-8") == revised_source  # type: ignore[attr-defined]


@pytest.mark.parametrize("version", [SCHEMA_VERSION - 1, SCHEMA_VERSION + 1])
def test_prd_content_refuses_incompatible_schema_without_migration(
    tmp_path: Path, version: int
) -> None:
    state_dir = _project(tmp_path)
    connection = sqlite3.connect(state_dir / "state.db")
    try:
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()
    before = _manifest(state_dir)
    with pytest.raises(PrdContentRefusal) as caught:
        read_prd_content(state_dir, "default")
    assert caught.value.error.code.value == "schema_incompatible"
    assert _manifest(state_dir) == before


def test_prd_show_malformed_database_never_exposes_raw_exception_or_path(
    tmp_path: Path,
) -> None:
    state_dir = _project(tmp_path)
    connection = sqlite3.connect(state_dir / "state.db")
    try:
        connection.execute("DROP TABLE prds")
        connection.commit()
    finally:
        connection.close()
    before = _manifest(state_dir)
    result = _show(tmp_path)
    assert result.exit_code == 1
    assert _payload(result)["error"]["code"] == "state_unavailable"
    assert str(tmp_path) not in result.stdout
    assert "sqlite" not in result.stdout.lower()
    assert _manifest(state_dir) == before


def test_prd_content_request_refusal_does_not_touch_unavailable_state(
    tmp_path: Path,
) -> None:
    with pytest.raises(PrdContentRefusal) as caught:
        read_prd_content(
            tmp_path / "missing",
            "default",
            expected_digest="not-a-digest",
        )
    assert caught.value.error.code.value == "invalid_request"
