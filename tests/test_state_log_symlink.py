"""State logs must never be appended through a symbolic link.

With the in-repo state layout, ``.anvil/`` can be repository content. A linked
``events.jsonl`` or ``audit.jsonl`` leaf would redirect engine writes to a file
outside the state directory.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from anvil.state.backend import SchemaProbeFailed, TransactionAborted
from anvil.state.models import EventDraft
from anvil.state.sqlite import SqliteBackend, _open_state_log_for_append

pytestmark = pytest.mark.skipif(
    not hasattr(os, "symlink") or os.name == "nt",
    reason="symbolic links require POSIX semantics",
)


def _project_event() -> EventDraft:
    now = datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC)
    return EventDraft(
        timestamp=now,
        actor="test",
        action="project.created",
        target_kind="project",
        target_id="proj-1",
        payload_json={
            "id": "proj-1",
            "name": "demo",
            "description": "",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
    )


def test_regular_log_append_still_works(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    with _open_state_log_for_append(log) as handle:
        handle.write("one\n")
    with _open_state_log_for_append(log) as handle:
        handle.write("two\n")
    assert log.read_text(encoding="utf-8") == "one\ntwo\n"


def test_log_inside_linked_directory_still_works(tmp_path: Path) -> None:
    real = tmp_path / "real-state"
    real.mkdir()
    (tmp_path / "state").symlink_to(real, target_is_directory=True)
    with _open_state_log_for_append(tmp_path / "state" / "events.jsonl") as handle:
        handle.write("one\n")
    assert (real / "events.jsonl").read_text(encoding="utf-8") == "one\n"


@pytest.mark.parametrize("target_exists", [True, False])
def test_linked_log_leaf_is_refused(tmp_path: Path, target_exists: bool) -> None:
    outside = tmp_path / "outside.txt"
    if target_exists:
        outside.write_text("keep\n", encoding="utf-8")
    log = tmp_path / "events.jsonl"
    log.symlink_to(outside)

    with pytest.raises(OSError):
        _open_state_log_for_append(log)

    if target_exists:
        assert outside.read_text(encoding="utf-8") == "keep\n"
    else:
        assert not outside.exists()


def test_leaf_swapped_for_a_link_after_the_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without O_NOFOLLOW (Windows) a link that appears after the pre-check is
    still refused, because the opened file is not the file at that name."""
    import os

    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    log = tmp_path / "events.jsonl"
    log.symlink_to(outside)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(os.path, "islink", lambda path: False)  # the stale check

    with pytest.raises(OSError):
        _open_state_log_for_append(log)

    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_engine_append_does_not_write_through_linked_event_log(
    tmp_path: Path, frozen_clock
) -> None:  # type: ignore[no-untyped-def]
    state = tmp_path / "state"
    state.mkdir()
    events = state / "events.jsonl"
    events.touch()
    backend = SqliteBackend(
        db_path=str(state / "state.db"), events_path=str(events), clock=frozen_clock
    )
    backend.initialize()
    try:
        outside = tmp_path / "outside.txt"
        outside.write_text("keep\n", encoding="utf-8")
        events.unlink()
        events.symlink_to(outside)

        # Current engines refuse at the append lock (the log is no longer the
        # regular file that was opened); the opener is the backstop behind it.
        with pytest.raises((TransactionAborted, SchemaProbeFailed)):
            backend.append(_project_event())

        assert outside.read_text(encoding="utf-8") == "keep\n"
    finally:
        backend.close()
