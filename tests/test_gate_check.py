"""Tests for ``anvil gate-check`` (B42 Phase 2 — the OpenClaw finish-gate decision).

``gate-check`` answers, for an agent about to finalize a turn: does this actor's
active claim have evidence satisfying its task's verification, or should the agent
be told to finish first? It is DEFAULT-OPEN (only blocks a genuinely
claimed-but-unverified task) and exit-coded (0 continue / 2 block / 1 error) so a
jq-less host can branch on ``$?``.

The decision matrix is exercised with stub backends (fast); the block/continue
paths AND the real backend method names (``list_active_claims`` / ``get_task`` /
``get_latest_evidence``) are pinned by integration tests against a real
``SqliteBackend`` — so a wrong method name can't be masked into a silently-inert
gate (the broad default-open behaviour would otherwise hide it).
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

from typer.testing import CliRunner

import anvil.cli.gate_check  # noqa: F401  (ensure submodule is in sys.modules)
from anvil.cli import app

# `anvil.cli` re-exports the `gate_check` FUNCTION, shadowing the submodule
# attribute, so grab the real module to patch its imported
# `_resolve_state_dir`/`_open_backend` names (the CLI-submodule gotcha).
gc_mod = sys.modules["anvil.cli.gate_check"]

runner = CliRunner()


# --- stubs --------------------------------------------------------------------


def _claim(actor: str = "agent", task_id: str = "WT-1", cid: str = "C1") -> object:
    return types.SimpleNamespace(claimed_by=actor, task_id=task_id, id=cid)


def _task(tid: str = "WT-1", required: tuple[str, ...] = ()) -> object:
    return types.SimpleNamespace(
        id=tid,
        verification=types.SimpleNamespace(required_evidence=list(required)),
    )


def _evidence(
    *,
    commands: tuple[str, ...] = (),
    pr_url: str | None = None,
    screenshots: tuple[str, ...] = (),
    files_changed: tuple[str, ...] = (),
    output_excerpt: str | None = None,
    known_limitations: str | None = None,
) -> object:
    return types.SimpleNamespace(
        commands_run=list(commands),
        pr_url=pr_url,
        screenshots=list(screenshots),
        files_changed=list(files_changed),
        output_excerpt=output_excerpt,
        known_limitations=known_limitations,
    )


class _StubBackend:
    """A minimal backend exposing exactly what gate-check reads."""

    def __init__(self, *, claims=(), task=None, evidence=None) -> None:
        self._claims = list(claims)
        self._task = task
        self._evidence = evidence

    def list_active_claims(self) -> list:
        return self._claims

    def get_task(self, _task_id: str):  # noqa: ANN201
        return self._task

    def get_latest_evidence(self, _task_id: str):  # noqa: ANN201
        return self._evidence

    def close(self) -> None:
        pass


class _NoCloseProxy:
    """Wrap a real backend so gate-check's ``finally: close()`` does not close the
    fixture-owned backend (the fixture owns its lifecycle)."""

    def __init__(self, real: object) -> None:
        self._real = real

    def __getattr__(self, name: str):  # noqa: ANN201
        return getattr(self._real, name)

    def close(self) -> None:
        pass


def _use(monkeypatch, backend: object, state_dir: Path) -> None:
    monkeypatch.setattr(gc_mod, "_resolve_state_dir", lambda cwd: state_dir)
    monkeypatch.setattr(gc_mod, "_open_backend", lambda sd: backend)


# --- decision matrix (stub backends) -----------------------------------------


def test_continue_when_evidence_complete(tmp_path, monkeypatch) -> None:
    # required "test output" satisfied by a pytest command => complete => continue.
    backend = _StubBackend(
        claims=[_claim()],
        task=_task("WT-1", required=("test output",)),
        evidence=_evidence(commands=("uv run pytest -q",)),
    )
    _use(monkeypatch, backend, tmp_path)
    r = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    data = json.loads(r.stdout)["data"]
    assert data["block"] is False
    assert data["action"] == "continue"
    assert data["evidence_gate"]["passed"] is True


def test_block_when_evidence_incomplete(tmp_path, monkeypatch) -> None:
    # required ["test output", "PR link"]; evidence has the test but no PR url.
    backend = _StubBackend(
        claims=[_claim()],
        task=_task("WT-1", required=("test output", "PR link")),
        evidence=_evidence(commands=("uv run pytest -q",)),
    )
    _use(monkeypatch, backend, tmp_path)
    r = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 2
    data = json.loads(r.stdout)["data"]
    assert data["block"] is True
    assert data["action"] == "revise"
    assert data["task"] == "WT-1"
    assert data["evidence_gate"]["missing"] == ["PR link"]
    assert "anvil submit WT-1" in data["instruction"]


def test_block_when_no_evidence_and_required(tmp_path, monkeypatch) -> None:
    # active claim, required evidence declared, NONE submitted => block (the
    # primary real-world case: agent about to finalize without submitting).
    backend = _StubBackend(
        claims=[_claim()],
        task=_task("WT-1", required=("test output",)),
        evidence=None,
    )
    _use(monkeypatch, backend, tmp_path)
    r = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 2
    data = json.loads(r.stdout)["data"]
    assert data["block"] is True
    assert data["evidence_gate"]["missing"] == ["test output"]


def test_no_claim_continues(tmp_path, monkeypatch) -> None:
    _use(monkeypatch, _StubBackend(claims=[]), tmp_path)
    r = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    data = json.loads(r.stdout)["data"]
    assert data["block"] is False
    assert data["task"] is None


def test_other_actor_claim_is_invisible(tmp_path, monkeypatch) -> None:
    # Only a human's claim exists; gating "agent" must not block it.
    _use(monkeypatch, _StubBackend(claims=[_claim(actor="alice")]), tmp_path)
    r = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    assert json.loads(r.stdout)["data"]["block"] is False


def test_quiet_block_is_exit_only(tmp_path, monkeypatch) -> None:
    backend = _StubBackend(
        claims=[_claim()],
        task=_task("WT-1", required=("test output",)),
        evidence=None,
    )
    _use(monkeypatch, backend, tmp_path)
    r = runner.invoke(app, ["gate-check", "-q", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 2
    assert r.stdout.strip() == ""


def test_quiet_continue_is_exit_only(tmp_path, monkeypatch) -> None:
    _use(monkeypatch, _StubBackend(claims=[]), tmp_path)
    r = runner.invoke(app, ["gate-check", "-q", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    assert r.stdout.strip() == ""


# --- default-open safety paths -----------------------------------------------


def test_not_initialized_continues(tmp_path) -> None:
    """A directory that is not a tracked anvil project is never a block."""
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        r = runner.invoke(app, ["gate-check", "--json"], catch_exceptions=False)
    finally:
        os.chdir(original)
    assert r.exit_code == 0
    assert json.loads(r.stdout)["data"]["block"] is False


def test_corrupt_db_continues(tmp_path) -> None:
    """A corrupt/unopenable state.db must never crash or block a finalize."""
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert runner.invoke(
            app, ["init", "--name", "GateCorrupt"], catch_exceptions=False
        ).exit_code == 0
        (tmp_path / ".anvil" / "state.db").write_bytes(b"not a sqlite database")
        r = runner.invoke(app, ["gate-check", "--json"], catch_exceptions=False)
    finally:
        os.chdir(original)
    assert r.exit_code == 0
    assert json.loads(r.stdout)["data"]["block"] is False


def test_state_root_invalid_emits_envelope(tmp_path, monkeypatch) -> None:
    """An invalid ANVIL_ROOT is a genuine error: ok=false envelope, exit 1."""
    from anvil.cli._helpers import StateRootError

    def _boom(_cwd):  # noqa: ANN202
        raise StateRootError("ANVIL_ROOT points at a non-directory")

    monkeypatch.setattr(gc_mod, "_resolve_state_dir", _boom)
    r = runner.invoke(app, ["gate-check", "--json"], catch_exceptions=False)
    assert r.exit_code == 1
    env = json.loads(r.stdout)
    assert env["ok"] is False
    assert env["error"]["code"] == "state_root_invalid"


# --- real SqliteBackend integration (pins backend method names + block path) --


def test_real_backend_blocks_claimed_unverified(
    approved_backend, frozen_clock, tmp_path, monkeypatch
) -> None:
    """End-to-end against a real backend: an agent's active claim on a task with
    declared required_evidence and NO submitted evidence => block (exit 2).

    This is the test that catches a wrong backend method name — it drives the
    real ``list_active_claims`` / ``get_task`` / ``get_latest_evidence``.
    """
    from anvil.claims.manager import ClaimManager
    from anvil.state.models import Verification
    from anvil.workflows.tasks import create_workflow_task

    tid = create_workflow_task(
        approved_backend, title="t", description="d", actor="agent",
        clock=frozen_clock,
        verification=Verification(commands=["pytest -q"], required_evidence=["test output"]),
    )
    ClaimManager(approved_backend, frozen_clock, actor="agent").claim(tid)

    _use(monkeypatch, _NoCloseProxy(approved_backend), tmp_path)
    r = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 2
    data = json.loads(r.stdout)["data"]
    assert data["block"] is True
    assert data["task"] == tid
    assert data["evidence_gate"]["missing"] == ["test output"]


def test_real_backend_continue_with_no_required_evidence(
    approved_backend, frozen_clock, tmp_path, monkeypatch
) -> None:
    """Real backend, agent holds a claim but the task requires no evidence =>
    continue, yet task/claim are resolved (proves the real read path ran)."""
    from anvil.claims.manager import ClaimManager
    from anvil.workflows.tasks import create_workflow_task

    tid = create_workflow_task(
        approved_backend, title="t", description="d", actor="agent", clock=frozen_clock,
    )
    claim = ClaimManager(approved_backend, frozen_clock, actor="agent").claim(tid).claim

    _use(monkeypatch, _NoCloseProxy(approved_backend), tmp_path)
    r = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    data = json.loads(r.stdout)["data"]
    assert data["block"] is False
    assert data["task"] == tid
    assert data["claim"] == claim.id
