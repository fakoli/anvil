"""Shared test fixtures for anvil Phase 2 test suite.

All fixtures use tmp_path (pytest's built-in per-test temp directory) so tests
are hermetically isolated and leave no on-disk state after completion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from anvil.clock import FrozenClock


@pytest.fixture
def frozen_clock() -> FrozenClock:
    """A FrozenClock fixed at 2026-05-24T18:00:00Z for deterministic tests."""
    return FrozenClock(datetime(2026, 5, 24, 18, 0, 0, tzinfo=UTC))


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """An empty temp directory to act as the project root."""
    return tmp_path


@pytest.fixture
def backend(state_dir: Path, frozen_clock: FrozenClock):  # type: ignore[no-untyped-def]
    """A fresh SqliteBackend initialized in tmp; cleaned up after test."""
    from anvil.state.sqlite import SqliteBackend

    db_path = str(state_dir / "state.db")
    events_path = str(state_dir / "events.jsonl")
    Path(events_path).touch()
    b = SqliteBackend(db_path=db_path, events_path=events_path, clock=frozen_clock)
    b.initialize()
    yield b
    b.close()
