"""Shared test fixtures for anvil Phase 2 test suite.

All fixtures use tmp_path (pytest's built-in per-test temp directory) so tests
are hermetically isolated and leave no on-disk state after completion.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anvil.clock import FrozenClock

GitRepoFactory = Callable[[Path], Path]


@pytest.fixture(scope="session")
def git_repo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the immutable committed repository copied by Git-heavy tests."""
    template = tmp_path_factory.mktemp("git-repo-template") / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(template)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=template,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=template,
        check=True,
        capture_output=True,
    )
    (template / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=template,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=template,
        check=True,
        capture_output=True,
    )
    return template


@pytest.fixture
def git_repo_factory(git_repo_template: Path) -> GitRepoFactory:
    """Return a physical copier that gives each test an independent repo."""

    def copy_repo(destination: Path) -> Path:
        if destination.exists() and any(destination.iterdir()):
            raise ValueError(f"Git fixture destination is not empty: {destination}")
        shutil.copytree(
            git_repo_template,
            destination,
            dirs_exist_ok=True,
            copy_function=shutil.copyfile,
        )
        return destination

    return copy_repo


@pytest.fixture
def git_repo(tmp_path: Path, git_repo_factory: GitRepoFactory) -> Path:
    """A per-test working copy of the immutable session Git template."""
    return git_repo_factory(tmp_path / "repo")


def append_exact_approved_prd(
    backend: Any,
    *,
    timestamp: datetime,
    project_id: str,
    prd_id: str,
    title: str,
    parsed_payload: dict[str, Any],
) -> None:
    """Append one current parse -> review -> approval test lineage."""
    import hashlib

    from anvil.planning.prd_persistence import material_content_sha256
    from anvil.state.models import EventDraft

    source = f"# Project: {title}\n"
    source_bytes = source.encode()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    material_sha256 = material_content_sha256(
        SimpleNamespace(
            source_bytes=source_bytes,
            markdown=source,
            source_sha256=source_sha256,
            source_size_bytes=len(source_bytes),
            source_encoding="utf-8",
        ),
        title,
    )
    parsed = {
        **parsed_payload,
        "project_id": project_id,
        "prd_id": prd_id,
        "title": title,
        "status": "draft",
        "expected_absent": True,
        "source_text": source,
        "source_sha256": source_sha256,
        "source_size_bytes": len(source_bytes),
        "source_encoding": "utf-8",
        "source_revision": 1,
        "provenance_state": "available",
        "content_available": True,
        "material_sha256": material_sha256,
    }
    content_event = backend.append(
        EventDraft(
            timestamp=timestamp,
            actor="seed",
            action="prd.parsed",
            target_kind="prd",
            target_id=prd_id,
            payload_json=parsed,
        )
    )
    assert content_event is not None
    review_event = backend.append(
        EventDraft(
            timestamp=timestamp,
            actor="reviewer",
            action="prd.reviewed",
            target_kind="prd",
            target_id=prd_id,
            payload_json={
                "project_id": project_id,
                "prd_id": prd_id,
                "reviewer": "reviewer",
                "binding_version": 1,
                "expected_revision": 1,
                "expected_status": "draft",
                "source_sha256": source_sha256,
                "material_sha256": material_sha256,
                "content_event_id": content_event.id,
            },
        )
    )
    assert review_event is not None
    backend.append(
        EventDraft(
            timestamp=timestamp,
            actor="approver",
            action="prd.approved",
            target_kind="prd",
            target_id=prd_id,
            payload_json={
                "project_id": project_id,
                "prd_id": prd_id,
                "approver": "approver",
                "binding_version": 1,
                "expected_revision": 1,
                "expected_status": "reviewed",
                "source_sha256": source_sha256,
                "material_sha256": material_sha256,
                "content_event_id": content_event.id,
                "review_event_id": review_event.id,
            },
        )
    )
    state_dir = Path(backend._events_path).parent  # noqa: SLF001
    source_path = (
        state_dir / "prd.md"
        if prd_id == "default"
        else state_dir / "prds" / f"{prd_id}.md"
    )
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(source_bytes)


def _explicitly_selects_live_github_args(args: Sequence[str]) -> bool:
    """Return whether raw pytest CLI arguments exactly select live tests."""
    expression: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        if arg == "-m":
            expression = args[index + 1] if index + 1 < len(args) else None
            index += 2
            continue
        if arg.startswith("-") and not arg.startswith("--"):
            short_options = arg[1:]
            marker_index = short_options.find("m")
            if marker_index >= 0 and set(short_options[:marker_index]) <= set("qvsx"):
                inline_value = short_options[marker_index + 1 :]
                if inline_value.startswith("="):
                    inline_value = inline_value[1:]
                expression = (
                    inline_value
                    if inline_value
                    else args[index + 1] if index + 1 < len(args) else None
                )
                index += 1 if inline_value else 2
                continue
        index += 1
    return expression is not None and expression.strip() == "live_github"


def _explicitly_selects_live_github(config: pytest.Config) -> bool:
    """Allow live tests only for the documented exact marker opt-in.

    Read only the arguments supplied to this pytest invocation.  The effective
    marker expression also includes ambient ``PYTEST_ADDOPTS`` and configuration,
    neither of which is deliberate authorization to mutate a real repository.
    Mirror pytest's last-option-wins behavior for ``-m VALUE``, ``-mVALUE``,
    and clusters of pytest's argument-free short flags (for example
    ``-qmlive_github``).  Stop at the option terminator and fail closed for
    every broader expression or unsupported spelling.
    """
    return _explicitly_selects_live_github_args(config.invocation_params.args)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Keep credentialed live tests opt-in under replacement CLI selectors."""
    if _explicitly_selects_live_github(config):
        return
    selected_items: list[pytest.Item] = []
    live_items: list[pytest.Item] = []
    for item in items:
        if item.get_closest_marker("live_github"):
            live_items.append(item)
        else:
            selected_items.append(item)
    if not live_items:
        return
    items[:] = selected_items
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
    import hashlib
    from types import SimpleNamespace

    from anvil.planning.prd_persistence import material_content_sha256
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
    source = (
        "# Project: P\n\n## Summary\nS.\n\n## Goals\n- G.\n\n"
        "## Requirements\n- R001: R.\n"
    )
    source_bytes = source.encode()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    material_sha256 = material_content_sha256(
        SimpleNamespace(
            source_bytes=source_bytes,
            markdown=source,
            source_sha256=source_sha256,
            source_size_bytes=len(source_bytes),
            source_encoding="utf-8",
        ),
        "P",
    )
    parsed_event = backend.append(_ev(
        "prd.parsed",
        {"project_id": "proj-1", "title": "P", "status": "draft", "summary": "S.",
         "goals": ["G."], "non_goals": [],
         "requirements": [{"id": "R001", "prd_section": "requirements",
                           "text": "R.", "source_paragraph": None, "derived": False}],
         "acceptance_criteria": ["AC."], "risks": [], "open_questions": [],
         "source_text": source, "source_sha256": source_sha256,
         "source_size_bytes": len(source_bytes), "source_encoding": "utf-8",
         "source_revision": 1, "provenance_state": "available",
         "content_available": True, "material_sha256": material_sha256},
        "prd", "proj-1",
    ))
    assert parsed_event is not None
    reviewed_event = backend.append(_ev(
        "prd.reviewed",
        {"project_id": "proj-1", "reviewer": "a", "binding_version": 1,
         "expected_revision": 1, "expected_status": "draft",
         "source_sha256": source_sha256, "material_sha256": material_sha256,
         "content_event_id": parsed_event.id},
        "prd", "proj-1",
    ))
    assert reviewed_event is not None
    backend.append(_ev(
        "prd.approved",
        {"project_id": "proj-1", "approver": "b", "binding_version": 1,
         "expected_revision": 1, "expected_status": "reviewed",
         "source_sha256": source_sha256, "material_sha256": material_sha256,
         "content_event_id": parsed_event.id, "review_event_id": reviewed_event.id},
        "prd", "proj-1",
    ))
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
