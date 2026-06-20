"""Tests for ``anvil claim-guard`` (B42 Phase 2 item 3 — the OpenClaw before-edit guard).

``claim-guard`` answers, for an agent about to mutate file(s): does this actor hold
an active claim (and does it cover the file)? It is DEFAULT-OPEN — only the
has-NO-claim case yields exit 2 (block); a claim that doesn't cover the file only
warns (exit 0), because ``expected_files`` is advisory. The node plugin maps the
verdict to allow / log / requireApproval / hard-block per its configured mode
(default ``warn``); the verb itself is mode-agnostic.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

from typer.testing import CliRunner

import anvil.cli.claim_guard  # noqa: F401  (ensure submodule is in sys.modules)
from anvil.cli import app

# `anvil.cli` re-exports the function, shadowing the submodule — grab the real
# module to patch its imported _resolve_state_dir / _open_backend (CLI gotcha).
cg_mod = sys.modules["anvil.cli.claim_guard"]

runner = CliRunner()


def _claim(actor: str = "agent", cid: str = "C1", expected_files: tuple[str, ...] = ()) -> object:
    return types.SimpleNamespace(claimed_by=actor, id=cid, expected_files=list(expected_files))


class _StubBackend:
    """Minimal backend — claim-guard only reads list_active_claims()."""

    def __init__(self, *, claims=(), raise_on_list: bool = False) -> None:
        self._claims = list(claims)
        self._raise = raise_on_list

    def list_active_claims(self) -> list:
        if self._raise:
            raise RuntimeError("simulated db read fault")
        return self._claims

    def close(self) -> None:
        pass


class _NoCloseProxy:
    def __init__(self, real: object) -> None:
        self._real = real

    def __getattr__(self, name: str):  # noqa: ANN201
        return getattr(self._real, name)

    def close(self) -> None:
        pass


def _use(monkeypatch, backend: object, state_dir: Path) -> None:
    monkeypatch.setattr(cg_mod, "_resolve_state_dir", lambda cwd: state_dir)
    monkeypatch.setattr(cg_mod, "_open_backend", lambda sd: backend)


# --- decision matrix (stub backends) -----------------------------------------


def test_no_claim_blocks(tmp_path, monkeypatch) -> None:
    _use(monkeypatch, _StubBackend(claims=[]), tmp_path)
    r = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent", "--file", "src/a.py"],
                      catch_exceptions=False)
    assert r.exit_code == 2
    data = json.loads(r.stdout)["data"]
    assert data["block"] is True
    assert data["action"] == "block"
    assert data["scope"] == "no_claim"
    assert data["has_claim"] is False


def test_other_actor_claim_only_blocks(tmp_path, monkeypatch) -> None:
    # Only a human's claim exists; the agent still has no claim → block.
    _use(monkeypatch, _StubBackend(claims=[_claim(actor="alice", expected_files=("x.py",))]), tmp_path)
    r = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent", "--file", "x.py"],
                      catch_exceptions=False)
    assert r.exit_code == 2
    assert json.loads(r.stdout)["data"]["scope"] == "no_claim"


def test_covering_claim_allows(tmp_path, monkeypatch) -> None:
    _use(monkeypatch, _StubBackend(claims=[_claim(expected_files=("src/a.py",))]), tmp_path)
    r = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent", "--file", "src/a.py"],
                      catch_exceptions=False)
    assert r.exit_code == 0
    data = json.loads(r.stdout)["data"]
    assert data["block"] is False
    assert data["action"] == "continue"
    assert data["scope"] == "covered"
    assert data["covered"] is True


def test_outside_scope_warns_not_blocks(tmp_path, monkeypatch) -> None:
    # Has a claim, but editing a file outside its declared scope → warn, NOT block.
    _use(monkeypatch, _StubBackend(claims=[_claim(expected_files=("src/a.py",))]), tmp_path)
    r = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent", "--file", "src/b.py"],
                      catch_exceptions=False)
    assert r.exit_code == 0  # advisory — never blocks
    data = json.loads(r.stdout)["data"]
    assert data["block"] is False
    assert data["action"] == "warn"
    assert data["scope"] == "outside_scope"


def test_has_claim_no_files_allows(tmp_path, monkeypatch) -> None:
    _use(monkeypatch, _StubBackend(claims=[_claim(expected_files=("x",))]), tmp_path)
    r = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    assert json.loads(r.stdout)["data"]["scope"] == "no_files"


def test_dotslash_normalization_both_directions(tmp_path, monkeypatch) -> None:
    _use(monkeypatch, _StubBackend(claims=[_claim(expected_files=("src/a.py",))]), tmp_path)
    r1 = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent", "--file", "./src/a.py"],
                       catch_exceptions=False)
    assert json.loads(r1.stdout)["data"]["covered"] is True
    _use(monkeypatch, _StubBackend(claims=[_claim(expected_files=("./src/a.py",))]), tmp_path)
    r2 = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent", "--file", "src/a.py"],
                       catch_exceptions=False)
    assert json.loads(r2.stdout)["data"]["covered"] is True


def test_multi_file_one_uncovered_warns(tmp_path, monkeypatch) -> None:
    _use(monkeypatch, _StubBackend(claims=[_claim(expected_files=("a.py",))]), tmp_path)
    r = runner.invoke(
        app, ["claim-guard", "--json", "--actor", "agent", "--file", "a.py", "--file", "b.py"],
        catch_exceptions=False,
    )
    assert r.exit_code == 0
    assert json.loads(r.stdout)["data"]["scope"] == "outside_scope"


def test_quiet_block_is_exit_only(tmp_path, monkeypatch) -> None:
    _use(monkeypatch, _StubBackend(claims=[]), tmp_path)
    r = runner.invoke(app, ["claim-guard", "-q", "--actor", "agent", "--file", "a.py"],
                      catch_exceptions=False)
    assert r.exit_code == 2
    assert r.stdout.strip() == ""


def test_quiet_allow_is_exit_only(tmp_path, monkeypatch) -> None:
    _use(monkeypatch, _StubBackend(claims=[_claim()]), tmp_path)
    r = runner.invoke(app, ["claim-guard", "-q", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    assert r.stdout.strip() == ""


def test_json_envelope_keys_present(tmp_path, monkeypatch) -> None:
    _use(monkeypatch, _StubBackend(claims=[]), tmp_path)
    r = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent", "--file", "a.py"],
                      catch_exceptions=False)
    out = r.stdout.strip()
    assert "\n" not in out  # single-line envelope (node parseEnvelope relies on it)
    data = json.loads(out)["data"]
    for key in ("block", "action", "actor", "files", "has_claim", "covered", "claim", "scope", "reason"):
        assert key in data


# --- default-open safety paths -----------------------------------------------


def test_not_initialized_allows(tmp_path) -> None:
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        r = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent", "--file", "a.py"],
                          catch_exceptions=False)
    finally:
        os.chdir(original)
    assert r.exit_code == 0
    assert json.loads(r.stdout)["data"]["block"] is False


def test_open_failure_allows(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cg_mod, "_resolve_state_dir", lambda cwd: tmp_path)

    def _boom(_sd):  # noqa: ANN202
        raise RuntimeError("cannot open db")

    monkeypatch.setattr(cg_mod, "_open_backend", _boom)
    r = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent", "--file", "a.py"],
                      catch_exceptions=False)
    assert r.exit_code == 0
    assert json.loads(r.stdout)["data"]["block"] is False


def test_read_fault_allows(tmp_path, monkeypatch) -> None:
    _use(monkeypatch, _StubBackend(raise_on_list=True), tmp_path)
    r = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent", "--file", "a.py"],
                      catch_exceptions=False)
    assert r.exit_code == 0  # read fault must never block a tool
    assert json.loads(r.stdout)["data"]["block"] is False


def test_state_root_invalid_emits_envelope(tmp_path, monkeypatch) -> None:
    from anvil.cli._helpers import StateRootError

    def _boom(_cwd):  # noqa: ANN202
        raise StateRootError("ANVIL_ROOT is invalid")

    monkeypatch.setattr(cg_mod, "_resolve_state_dir", _boom)
    r = runner.invoke(app, ["claim-guard", "--json"], catch_exceptions=False)
    assert r.exit_code == 1
    env = json.loads(r.stdout)
    assert env["ok"] is False
    assert env["error"]["code"] == "state_root_invalid"


# --- real SqliteBackend integration (pins claimed_by/expected_files reads) ----


def test_real_backend_blocks_when_no_claim(approved_backend, frozen_clock, tmp_path, monkeypatch) -> None:
    """Real backend, task exists but the actor holds NO claim → block (exit 2).
    Drives the real list_active_claims so a renamed field can't silently allow."""
    from anvil.workflows.tasks import create_workflow_task

    create_workflow_task(
        approved_backend, title="t", description="d", actor="agent", clock=frozen_clock,
    )  # task created but NOT claimed
    _use(monkeypatch, _NoCloseProxy(approved_backend), tmp_path)
    r = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent", "--file", "src/x.py"],
                      catch_exceptions=False)
    assert r.exit_code == 2
    assert json.loads(r.stdout)["data"]["scope"] == "no_claim"


def test_real_backend_allows_with_active_claim(approved_backend, frozen_clock, tmp_path, monkeypatch) -> None:
    """Real backend, actor holds an active claim → allow (exit 0)."""
    from anvil.claims.manager import ClaimManager
    from anvil.workflows.tasks import create_workflow_task

    tid = create_workflow_task(
        approved_backend, title="t", description="d", actor="agent", clock=frozen_clock,
    )
    ClaimManager(approved_backend, frozen_clock, actor="agent").claim(tid)
    _use(monkeypatch, _NoCloseProxy(approved_backend), tmp_path)
    r = runner.invoke(app, ["claim-guard", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    assert json.loads(r.stdout)["data"]["has_claim"] is True
