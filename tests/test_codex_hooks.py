"""Tests for the B41 Codex/cross-harness hook verbs: `anvil hook stop-gate` and
`anvil hook heartbeat`.

`stop-gate` is the OPT-IN Stop-hook evidence gate — it reuses `gate-check`'s
`decide_from_rows`, so a block here must agree with `gate-check` (parity). It is
default-OPEN, loop-guarded via `stop_hook_active`, and emits Codex's
`{"decision":"block",...}` + exit 2 on a block. `heartbeat` renews the actor's
active claim lease(s) on tool activity and always exits 0.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

from typer.testing import CliRunner

import anvil.cli.hooks  # noqa: F401
from anvil.cli import app

hooks_mod = sys.modules["anvil.cli.hooks"]
runner = CliRunner()


def _claim(actor: str = "agent", cid: str = "C1", task_id: str = "WT-1") -> object:
    return types.SimpleNamespace(claimed_by=actor, id=cid, task_id=task_id, expected_files=[])


def _task(tid: str = "WT-1", required: tuple[str, ...] = ()) -> object:
    return types.SimpleNamespace(
        id=tid, verification=types.SimpleNamespace(required_evidence=list(required)),
    )


class _StubBackend:
    def __init__(self, *, claims=(), task=None, evidence=None) -> None:
        self._claims = list(claims)
        self._task = task
        self._evidence = evidence

    def list_active_claims(self) -> list:
        return self._claims

    def get_task(self, _tid):  # noqa: ANN001, ANN201
        return self._task

    def get_latest_evidence(self, _tid):  # noqa: ANN001, ANN201
        return self._evidence

    def close(self) -> None:
        pass


class _NoCloseProxy:
    def __init__(self, real: object) -> None:
        self._real = real

    def __getattr__(self, name: str):  # noqa: ANN201
        return getattr(self._real, name)

    def close(self) -> None:
        pass


def _use_backend(monkeypatch, backend: object, state_dir: Path) -> None:
    # _resolve_state_dir is module-top in hooks.py; _open_backend is lazily imported
    # from anvil.cli._helpers inside the verb, so patch it at the source module.
    monkeypatch.setattr(hooks_mod, "_resolve_state_dir", lambda cwd: state_dir)
    import anvil.cli._helpers as helpers
    monkeypatch.setattr(helpers, "_open_backend", lambda sd: backend)


# --- stop-gate ---------------------------------------------------------------


def test_stop_gate_allows_when_no_claim(tmp_path, monkeypatch) -> None:
    _use_backend(monkeypatch, _StubBackend(claims=[]), tmp_path)
    r = runner.invoke(app, ["hook", "stop-gate", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0


def test_stop_gate_blocks_claimed_unverified(tmp_path, monkeypatch) -> None:
    backend = _StubBackend(
        claims=[_claim()], task=_task("WT-1", required=("test output",)), evidence=None,
    )
    _use_backend(monkeypatch, backend, tmp_path)
    r = runner.invoke(app, ["hook", "stop-gate", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 2
    out = json.loads(r.stdout.strip().splitlines()[0])
    assert out["decision"] == "block"
    assert out["reason"]  # non-empty continuation prompt


def test_stop_gate_loop_guard(tmp_path, monkeypatch) -> None:
    # stop_hook_active in the payload → never re-block, even with an unverified claim.
    backend = _StubBackend(
        claims=[_claim()], task=_task("WT-1", required=("test output",)), evidence=None,
    )
    _use_backend(monkeypatch, backend, tmp_path)
    r = runner.invoke(
        app, ["hook", "stop-gate", "--actor", "agent"],
        input='{"stop_hook_active": true}', catch_exceptions=False,
    )
    assert r.exit_code == 0


def test_stop_gate_default_open_when_not_initialized(tmp_path) -> None:
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        r = runner.invoke(app, ["hook", "stop-gate", "--actor", "agent"], catch_exceptions=False)
    finally:
        os.chdir(original)
    assert r.exit_code == 0


def test_stop_gate_default_open_on_backend_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hooks_mod, "_resolve_state_dir", lambda cwd: tmp_path)
    import anvil.cli._helpers as helpers

    def _boom(_sd):  # noqa: ANN202
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(helpers, "_open_backend", _boom)
    r = runner.invoke(app, ["hook", "stop-gate", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0  # never break the turn


def test_stop_gate_parity_with_gate_check(tmp_path, monkeypatch) -> None:
    """The same claimed-unverified fixture must block in BOTH gate-check (exit 2)
    and stop-gate (exit 2) — guards the shared decide_from_rows refactor."""
    backend = _StubBackend(
        claims=[_claim()], task=_task("WT-1", required=("test output",)), evidence=None,
    )
    _use_backend(monkeypatch, backend, tmp_path)
    sg = runner.invoke(app, ["hook", "stop-gate", "--actor", "agent"], catch_exceptions=False)
    # gate-check resolves via its OWN module's helpers — patch those too. (anvil.cli
    # re-exports the gate_check FUNCTION, so grab the real module via sys.modules.)
    import anvil.cli.gate_check  # noqa: F401
    gc = sys.modules["anvil.cli.gate_check"]
    monkeypatch.setattr(gc, "_resolve_state_dir", lambda cwd: tmp_path)
    monkeypatch.setattr(gc, "_open_backend", lambda sd: backend)
    gck = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    assert sg.exit_code == 2 and gck.exit_code == 2


# --- heartbeat ---------------------------------------------------------------


def test_heartbeat_no_claim_is_noop(tmp_path, monkeypatch) -> None:
    _use_backend(monkeypatch, _StubBackend(claims=[]), tmp_path)
    r = runner.invoke(app, ["hook", "heartbeat", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0


def test_heartbeat_not_initialized_is_noop(tmp_path) -> None:
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        r = runner.invoke(app, ["hook", "heartbeat", "--actor", "agent"], catch_exceptions=False)
    finally:
        os.chdir(original)
    assert r.exit_code == 0


def test_heartbeat_renews_active_claim(approved_backend, frozen_clock, tmp_path, monkeypatch) -> None:
    """Real backend: heartbeat renews the actor's active claim (spy proves renew
    was invoked for the right claim) and exits 0."""
    from anvil.claims.manager import ClaimManager
    from anvil.workflows.tasks import create_workflow_task

    tid = create_workflow_task(
        approved_backend, title="t", description="d", actor="agent", clock=frozen_clock,
    )
    claim = ClaimManager(approved_backend, frozen_clock, actor="agent").claim(tid).claim

    renewed: list[str] = []
    orig_renew = ClaimManager.renew

    def _spy(self, claim_id):  # noqa: ANN001, ANN202
        renewed.append(claim_id)
        return orig_renew(self, claim_id)

    monkeypatch.setattr("anvil.claims.manager.ClaimManager.renew", _spy)
    _use_backend(monkeypatch, _NoCloseProxy(approved_backend), tmp_path)
    r = runner.invoke(app, ["hook", "heartbeat", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    assert claim.id in renewed  # the actor's active claim was renewed
