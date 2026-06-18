"""Shared test fixtures for anvil Phase 2 test suite.

All fixtures use tmp_path (pytest's built-in per-test temp directory) so tests
are hermetically isolated and leave no on-disk state after completion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from anvil.clock import FrozenClock


@pytest.fixture(autouse=True)
def _full_mcp_surface():  # type: ignore[no-untyped-def]
    """Isolate every test from the process-global MCP visibility transforms.

    The MCP server is a process-global ``FastMCP`` singleton (``anvil.mcp_server.
    mcp``). The L2 planning-surface gate (``apply_surface_gate`` / ``main()``)
    hides the planning tools by APPENDING a visibility transform to
    ``mcp._transforms``. FastMCP's ``disable()``/``enable()`` stack transforms
    (they never pop), so without isolation:

    * a test exercising the startup gate would leave planning tools hidden,
      breaking later tests that call a planning tool; and
    * transforms would accumulate across the whole suite (~2 per test × ~1.7k
      tests), eventually blowing the recursion limit when ``list_tools`` walks
      the transform chain.

    Snapshotting and restoring the transform list around each test fixes both:
    every test starts from the full surface (whatever transforms existed at
    import time, i.e. none) and any transform a test adds is dropped afterward —
    zero accumulation, full isolation. Tests that assert gated behaviour call
    ``apply_surface_gate`` inside the test body; this fixture cleans up.
    """
    from anvil.mcp_server import mcp

    saved = list(mcp._transforms)
    try:
        yield
    finally:
        mcp._transforms[:] = saved


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
