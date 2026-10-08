"""Complete import, stable source identity and the real capture/append boundary."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
import typer

from anvil.claims.evidence_import import (
    CommandProofImportOverflow,
    inspect_command_buffer,
    require_buffer_unchanged,
)
from anvil.cli import hooks
from anvil.clock import SystemClock
from anvil.state.sqlite import SqliteBackend
from tests.test_native_evidence_correction import _buffer
from tests.test_strict_evidence import _PLANNED_VERIFY_CMD, _invoke, _planned


@pytest.mark.parametrize("count", [0, 15, 16, 17, 39])
def test_complete_inspection_preserves_failure_and_refuses_overflow(tmp_path, count):
    path = _buffer(tmp_path, count, failed=True)
    before = path.read_bytes()
    if count > 16:
        with pytest.raises(CommandProofImportOverflow, match="record limit") as error:
            inspect_command_buffer(tmp_path, "C00000001")
        incomplete = error.value.inspection
        assert incomplete.status == "incomplete" and incomplete.proofs == ()
        assert incomplete.inspected_records == 17
        assert incomplete.inspected_bytes == len(before)
    else:
        result = inspect_command_buffer(tmp_path, "C00000001")
        assert result.status == "complete"
        assert result.inspected_bytes == len(before)
        assert len(result.proofs) == count
        if count:
            assert result.proofs[0].exit_code == 1
    assert path.read_bytes() == before


def test_missing_empty_and_ineligible_inputs_have_distinct_facts(tmp_path):
    assert inspect_command_buffer(tmp_path, "C00000001").status == "missing"
    assert inspect_command_buffer(tmp_path, "C001").status == "ineligible"
    path = _buffer(tmp_path, 0)
    path.write_bytes(b"")
    empty = inspect_command_buffer(tmp_path, "C00000001")
    assert empty.status == "complete" and empty.source_sha256 is not None
    assert empty.inspected_bytes == empty.inspected_records == 0


def test_skipped_records_are_counted_without_becoming_proofs(tmp_path):
    path = _buffer(tmp_path, 1, failed=True)
    record = json.loads(path.read_text().splitlines()[0])
    bad_digest = {**record, "semantic_digest": "a" * 64}
    other = {**record, "claim_id": "C00000002"}
    suffix = b"\nnot-json\n\xff\n[]\n" + (
        json.dumps(bad_digest) + "\n" + json.dumps(other) + "\n"
    ).encode()
    path.write_bytes(path.read_bytes() + suffix)
    before = path.read_bytes()
    result = inspect_command_buffer(tmp_path, "C00000001")
    assert len(result.proofs) == 1 and result.proofs[0].exit_code == 1
    assert dict(result.skipped) == {"empty": 1, "invalid": 3, "not_object": 1, "other_claim": 1}
    assert result.inspected_records == 7
    assert path.read_bytes() == before


def test_exact_byte_bound_and_buffer_drift_refuse_without_rewriting(tmp_path):
    path = _buffer(tmp_path, 1)
    before = path.read_bytes()
    result = inspect_command_buffer(tmp_path, "C00000001", max_bytes=len(before))
    path.write_bytes(before + b"not-json\n")
    changed = path.read_bytes()
    with pytest.raises(CommandProofImportOverflow, match="byte limit"):
        inspect_command_buffer(tmp_path, "C00000001", max_bytes=len(before))
    with pytest.raises(CommandProofImportOverflow, match="changed before submission"):
        require_buffer_unchanged(tmp_path, "C00000001", result)
    assert path.read_bytes() == changed


def test_existing_nonregular_and_unreadable_sources_refuse(tmp_path, monkeypatch):
    path = _buffer(tmp_path, 1)
    path.unlink()
    path.mkdir()
    with pytest.raises(CommandProofImportOverflow, match="regular file"):
        inspect_command_buffer(tmp_path, "C00000001")
    path.rmdir()
    path = _buffer(tmp_path, 1)
    import anvil.claims.evidence_import as module

    original = module.os.open

    def denied(candidate, *args, **kwargs):
        if Path(candidate) == path:
            raise PermissionError("private error detail")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", denied)
    with pytest.raises(CommandProofImportOverflow, match="cannot be read completely") as error:
        inspect_command_buffer(tmp_path, "C00000001")
    assert "private error detail" not in str(error.value)


def test_source_replacement_during_read_refuses(tmp_path, monkeypatch):
    path = _buffer(tmp_path, 1)
    original_stat = Path.stat
    observed = 0

    def replacing(candidate, *args, **kwargs):
        nonlocal observed
        if candidate == path:
            observed += 1
            if observed == 2:
                replacement = path.with_suffix(".replacement")
                replacement.write_bytes(path.read_bytes())
                replacement.replace(path)
        return original_stat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", replacing)
    with pytest.raises(CommandProofImportOverflow, match="changed during complete import"):
        inspect_command_buffer(tmp_path, "C00000001")


def test_file_and_directory_symlinks_refuse_without_target_reads(tmp_path):
    path = _buffer(tmp_path, 1)
    target = tmp_path / "target"
    path.replace(target)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("native symlink creation unavailable")
    original = target.read_bytes()
    with pytest.raises(CommandProofImportOverflow, match="regular file"):
        inspect_command_buffer(tmp_path, "C00000001")
    path.unlink()
    path.parent.rmdir()
    directory = tmp_path / "outside"
    directory.mkdir()
    path.parent.symlink_to(directory, target_is_directory=True)
    with pytest.raises(CommandProofImportOverflow, match="directory is invalid"):
        inspect_command_buffer(tmp_path, "C00000001")
    assert target.read_bytes() == original


def _claim(tmp_path):
    task = _planned(tmp_path)
    result = _invoke(tmp_path, ["claim", task, "--actor", "agent-alpha", "--json"])
    assert result.exit_code == 0, result.output
    return task, json.loads(result.output)["data"]["claim"]["id"]


def _capture(tmp_path, *, failed=False):
    try:
        hooks.hook_capture_evidence(
            command=_PLANNED_VERIFY_CMD, exit_code=int(failed), actor="agent-alpha",
            stdout_file=None, stderr_file=None, output_sha256=None, cwd=tmp_path,
        )
    except typer.Exit as exc:
        assert exc.exit_code == 0


@pytest.mark.parametrize("tail", [b"\n", b"", b'\n{"partial":', None])
def test_capture_preserves_eof_and_separates_next_record(tmp_path, monkeypatch, tail):
    _, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    _capture(tmp_path, failed=True)
    state = tmp_path / ".anvil"
    path = state / ".evidence-buffer" / f"{claim}.json"
    before = path.read_bytes().removesuffix(b"\n") + tail if tail is not None else b""
    path.write_bytes(before)
    expected = [1] if before else []
    assert [proof.exit_code for proof in inspect_command_buffer(state, claim).proofs] == expected
    _capture(tmp_path)
    after = path.read_bytes()
    separator = b"" if not before or before.endswith(b"\n") else b"\n"
    assert after.startswith(before + separator)
    added = after[len(before + separator):]
    assert added.count(b"\n") == 1 and json.loads(added)["exit_code"] == 0
    result = inspect_command_buffer(state, claim)
    assert [proof.exit_code for proof in result.proofs] == expected + [0]
    assert dict(result.skipped) == ({"invalid": 1} if tail and b"partial" in tail else {})


@pytest.mark.parametrize("ancestor", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_capture_parent_swap_never_writes_foreign_file(
    tmp_path, monkeypatch, ancestor, existing,
):
    _, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    directory = tmp_path / ".anvil" / ".evidence-buffer"
    directory.mkdir()
    basename = f"{claim}.json"
    if existing:
        _capture(tmp_path, failed=True)
    parent = directory.parent if ancestor else directory
    preserved = parent.with_name(parent.name + "-preserved")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    target_dir = foreign / ".evidence-buffer" if ancestor else foreign
    target_dir.mkdir(exist_ok=True)
    target = target_dir / basename
    if existing:
        target.write_bytes(b"foreign bytes")
    original_open = hooks.os.open
    attempted = False
    refused = False

    def racing(candidate, *args, **kwargs):
        nonlocal attempted, refused
        if Path(candidate).name == basename and not attempted:
            attempted = True
            try:
                parent.rename(preserved)
            except OSError:
                if os.name != "nt":
                    raise
                refused = True
            else:
                parent.symlink_to(foreign, target_is_directory=True)
        return original_open(candidate, *args, **kwargs)

    # POSIX follows the real dir_fd open; Windows attempts a real rename while
    # the native directory handles must deny it. Neither path fakes custody.
    monkeypatch.setattr(hooks.os, "open", racing)
    _capture(tmp_path)
    assert attempted
    if existing:
        assert target.read_bytes() == b"foreign bytes"
    else:
        assert not target.exists()
    if os.name == "nt":
        assert refused
        assert [proof.exit_code for proof in inspect_command_buffer(
            tmp_path / ".anvil", claim,
        ).proofs] == ([1, 0] if existing else [0])


def test_capture_waits_for_native_append_lock_and_keeps_failed_result(tmp_path, monkeypatch):
    _, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    _capture(tmp_path)
    state = tmp_path / ".anvil"
    before = inspect_command_buffer(state, claim)
    backend = SqliteBackend(
        db_path=str(state / "state.db"), events_path=str(state / "events.jsonl"),
        clock=SystemClock(),
    )
    backend.initialize()
    started, done = threading.Event(), threading.Event()
    failures = []

    def writer():
        started.set()
        try:
            _capture(tmp_path, failed=True)
        except Exception as exc:
            failures.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=writer)
    try:
        with backend.claim_operation_lock():
            thread.start()
            assert started.wait(5)
            assert not done.wait(0.1)
            assert inspect_command_buffer(state, claim) == before
        thread.join(10)
        assert done.is_set() and not failures
        after = inspect_command_buffer(state, claim)
        assert [proof.exit_code for proof in after.proofs] == [0, 1]
    finally:
        backend.close()
        thread.join(10)


def test_ordinary_submit_rechecks_buffer_before_any_state_mutation(tmp_path, monkeypatch):
    task, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    _capture(tmp_path)
    state = tmp_path / ".anvil"
    buffer = state / ".evidence-buffer" / f"{claim}.json"
    events = (state / "events.jsonl").read_bytes()
    original = buffer.read_bytes()
    import anvil.cli.packet_apply as module

    inspector = module.inspect_command_buffer

    def inspected_then_changed(*args, **kwargs):
        result = inspector(*args, **kwargs)
        buffer.write_bytes(original + original)
        return result

    monkeypatch.setattr(module, "inspect_command_buffer", inspected_then_changed)
    result = _invoke(tmp_path, [
        "submit", task, "--commands", _PLANNED_VERIFY_CMD,
        "--files-changed", "src/foo.py", "--actor", "agent-alpha", "--json",
    ])
    assert result.exit_code == 1, result.output
    assert "command_proof_import_overflow" in result.output
    assert (state / "events.jsonl").read_bytes() == events
    assert buffer.read_bytes() == original + original


def test_capture_never_appends_through_orphan_symlink(tmp_path):
    assert _invoke(tmp_path, ["init"]).exit_code == 0
    target = tmp_path / "private-target"
    target.write_bytes(b"preserved")
    directory = tmp_path / ".anvil" / ".evidence-buffer"
    directory.mkdir()
    try:
        (directory / "orphan.json").symlink_to(target)
    except OSError:
        pytest.skip("native symlink creation unavailable")
    _capture(tmp_path, failed=True)
    assert target.read_bytes() == b"preserved"
