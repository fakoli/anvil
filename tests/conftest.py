"""Shared test fixtures for anvil Phase 2 test suite.

All fixtures use tmp_path (pytest's built-in per-test temp directory) so tests
are hermetically isolated and leave no on-disk state after completion.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from itertools import product
from pathlib import Path

import pytest
from _pytest.mark.expression import Expression

from anvil.clock import FrozenClock


def _command_line_mark_expression(config: pytest.Config) -> str | None:
    """Return only the explicit CLI ``-m`` expression, excluding addopts."""
    args = [str(arg) for arg in config.invocation_params.args]
    for index, arg in enumerate(args):
        if arg == "-m" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("-m="):
            return arg.removeprefix("-m=")
    return None


def _positively_selects_live_github(expression: str | None) -> bool:
    """Whether an explicit marker expression opts into ``live_github``.

    Pytest replaces, rather than composes, the configured ``-m`` expression
    when the CLI supplies another one. We therefore keep live tests excluded
    unless the command-line expression semantically selects them positively.
    """
    if expression is None:
        return False
    identifiers = {
        token
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expression)
        if token not in {"and", "or", "not"}
    }
    if "live_github" not in identifiers:
        return False
    compiled = Expression.compile(expression)
    others = sorted(identifiers - {"live_github"})
    # Marker expressions are normally tiny; fail closed instead of allowing a
    # user-supplied expression to trigger exponential collection work.
    if len(others) > 8:
        return False
    for combination in product((False, True), repeat=len(others)):
        values = dict(zip(others, combination, strict=True))

        def evaluate(live_github: bool) -> bool:
            return compiled.evaluate(
                lambda identifier: live_github
                if identifier == "live_github"
                else values[identifier]
            )

        if evaluate(True):
            return True
    return False


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Keep credentialed live tests opt-in under replacement CLI selectors."""
    expression = _command_line_mark_expression(config)
    if _positively_selects_live_github(expression):
        return
    live_items = [item for item in items if item.get_closest_marker("live_github")]
    if not live_items:
        return
    items[:] = [item for item in items if item not in live_items]
    config.hook.pytest_deselected(items=live_items)


@pytest.fixture(autouse=True)
def _local_state_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Pin the in-repo (`<cwd>/.anvil`) state layout for the whole suite.

    Production defaults to the HOME workspace (`~/.anvil/workspaces/<repo>/`), but
    the tests' cwd-relative fixtures (chdir into tmp_path, assert `tmp/.anvil`)
    assume the legacy local layout. Setting ANVIL_STATE_LAYOUT=local keeps every
    existing test correct AND stops tests from writing into the real ~/.anvil/.

    Also redirect ANVIL_KEYS_DIR (B48 part 2 signing) to a per-test temp dir so
    accepting a task never writes an Ed25519 keypair into the real ~/.anvil/keys/.
    """
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "local")
    keys = tmp_path_factory.mktemp("anvil-keys")
    monkeypatch.setenv("ANVIL_KEYS_DIR", str(keys))


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


@pytest.fixture
def approved_backend(backend, frozen_clock):  # type: ignore[no-untyped-def]
    """A backend with project + state + an APPROVED PRD — ready for claims.

    Shared by the WF-3 task/runner tests, which need to claim tasks (the claim
    gate requires an approved PRD).
    """
    from anvil.state.models import EventDraft

    t0 = frozen_clock.now()

    def _ev(action, payload, kind, tid):  # type: ignore[no-untyped-def]
        return EventDraft(
            timestamp=t0, actor="test", action=action,
            target_kind=kind, target_id=tid, payload_json=payload,
        )

    backend.append(_ev(
        "project.created",
        {"id": "proj-1", "name": "P", "description": "",
         "created_at": t0.isoformat(), "updated_at": t0.isoformat()},
        "project", "proj-1",
    ))
    backend.append(_ev("state.initialized", {}, "project", "proj-1"))
    backend.append(_ev(
        "prd.parsed",
        {"project_id": "proj-1", "status": "draft", "summary": "S.",
         "goals": ["G."], "non_goals": [],
         "requirements": [{"id": "R001", "prd_section": "requirements",
                           "text": "R.", "source_paragraph": None, "derived": False}],
         "acceptance_criteria": ["AC."], "risks": [], "open_questions": []},
        "prd", "proj-1",
    ))
    backend.append(_ev("prd.reviewed", {"project_id": "proj-1", "reviewer": "a"}, "prd", "proj-1"))
    backend.append(_ev("prd.approved", {"project_id": "proj-1", "approver": "b"}, "prd", "proj-1"))
    return backend


@pytest.fixture(autouse=True)
def _scrub_session_env(monkeypatch):
    """The distinct-actor fail-fast (schema v10) resolves ANVIL_SESSION_ID /
    CLAUDE_CODE_SESSION_ID from the environment. The suite frequently runs
    INSIDE a harness session where those are set, which would couple test
    behavior to ambient env (the gate silently exercised with a constant
    ambient session instead of the intended NULL-session default). Scrub both;
    tests that exercise the gate set their own via monkeypatch.setenv."""
    monkeypatch.delenv("ANVIL_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
