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


def _task(
    tid: str = "WT-1",
    required: tuple[str, ...] = (),
    required_proofs: tuple[object, ...] = (),
) -> object:
    return types.SimpleNamespace(
        id=tid,
        verification=types.SimpleNamespace(
            required_evidence=list(required),
            required_proofs=list(required_proofs),
        ),
    )


def _evidence(
    *,
    commands: tuple[str, ...] = (),
    pr_url: str | None = None,
    screenshots: tuple[str, ...] = (),
    files_changed: tuple[str, ...] = (),
    output_excerpt: str | None = None,
    known_limitations: str | None = None,
    proofs: tuple[object, ...] = (),
) -> object:
    return types.SimpleNamespace(
        commands_run=list(commands),
        pr_url=pr_url,
        screenshots=list(screenshots),
        files_changed=list(files_changed),
        output_excerpt=output_excerpt,
        known_limitations=known_limitations,
        proofs=list(proofs),
    )


class _StubBackend:
    """A minimal backend exposing exactly what gate-check reads.

    Single-claim tests pass ``task=``/``evidence=`` (returned for any id);
    multi-claim tests pass ``tasks=``/``evidence_map=`` dicts keyed by task id.
    """

    def __init__(
        self, *, claims=(), task=None, evidence=None, tasks=None, evidence_map=None
    ) -> None:
        self._claims = list(claims)
        self._task = task
        self._evidence = evidence
        self._tasks = tasks
        self._evidence_map = evidence_map

    def list_active_claims(self) -> list:
        return self._claims

    def get_task(self, task_id: str):  # noqa: ANN201
        if self._tasks is not None:
            return self._tasks.get(task_id)
        return self._task

    def get_latest_evidence(self, task_id: str):  # noqa: ANN201
        if self._evidence_map is not None:
            return self._evidence_map.get(task_id)
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
    continue (exit 0). Exercises the real read path + the empty-required pass."""
    from anvil.claims.manager import ClaimManager
    from anvil.workflows.tasks import create_workflow_task

    tid = create_workflow_task(
        approved_backend, title="t", description="d", actor="agent", clock=frozen_clock,
    )
    ClaimManager(approved_backend, frozen_clock, actor="agent").claim(tid)

    _use(monkeypatch, _NoCloseProxy(approved_backend), tmp_path)
    r = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    data = json.loads(r.stdout)["data"]
    assert data["block"] is False
    assert data["task"] is None  # nothing blocks → no task reported


# --- review fixes: multi-claim, cwd-scope, human emit, actor default ----------


def test_blocks_when_one_of_several_claims_incomplete(tmp_path, monkeypatch) -> None:
    """Actor holds two claims; WT-1 complete, WT-2 not => block on WT-2 (the gate
    must check ALL of an actor's claims, not just the first — anvil does not cap
    claims per actor)."""
    backend = _StubBackend(
        claims=[_claim(task_id="WT-1", cid="C1"), _claim(task_id="WT-2", cid="C2")],
        tasks={
            "WT-1": _task("WT-1", required=("test output",)),
            "WT-2": _task("WT-2", required=("PR link",)),
        },
        evidence_map={"WT-1": _evidence(commands=("uv run pytest -q",))},  # WT-2: none
    )
    _use(monkeypatch, backend, tmp_path)
    r = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 2
    data = json.loads(r.stdout)["data"]
    assert data["block"] is True
    assert data["task"] == "WT-2"
    assert data["evidence_gate"]["missing"] == ["PR link"]


def test_block_picks_first_incomplete_deterministically(tmp_path, monkeypatch) -> None:
    """Two incomplete claims, claim order reversed => reports the task-id-sorted
    first (WT-1) and notes the others (list_active_claims has no ORDER BY)."""
    backend = _StubBackend(
        claims=[_claim(task_id="WT-2", cid="C2"), _claim(task_id="WT-1", cid="C1")],
        tasks={
            "WT-1": _task("WT-1", required=("PR link",)),
            "WT-2": _task("WT-2", required=("test output",)),
        },
        evidence_map={},  # neither has evidence
    )
    _use(monkeypatch, backend, tmp_path)
    r = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 2
    data = json.loads(r.stdout)["data"]
    assert data["task"] == "WT-1"  # sorted, despite reversed claim order
    assert "1 other" in data["instruction"]


def test_active_claim_without_task_row_continues(tmp_path, monkeypatch) -> None:
    """A claim whose task get_task can't find must not block (anomalous)."""
    backend = _StubBackend(claims=[_claim(task_id="GONE")], tasks={}, evidence_map={})
    _use(monkeypatch, backend, tmp_path)
    r = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    assert json.loads(r.stdout)["data"]["block"] is False


def test_cwd_flag_is_forwarded_to_resolver(tmp_path, monkeypatch) -> None:
    """--cwd is forwarded to _resolve_state_dir — the mechanism that makes the
    plugin's explicit --cwd win over a Gateway-level ANVIL_ROOT (the no-false-block
    fix). _resolve_state_dir ignores ANVIL_ROOT when given an explicit cwd."""
    seen = {}

    def _spy(cwd):  # noqa: ANN202
        seen["cwd"] = cwd
        return tmp_path  # exists

    monkeypatch.setattr(gc_mod, "_resolve_state_dir", _spy)
    monkeypatch.setattr(gc_mod, "_open_backend", lambda sd: _StubBackend(claims=[]))
    r = runner.invoke(
        app, ["gate-check", "--json", "--cwd", "/proj/x", "--actor", "agent"],
        catch_exceptions=False,
    )
    assert r.exit_code == 0
    assert str(seen["cwd"]) == "/proj/x"


def test_human_block_prints_instruction(tmp_path, monkeypatch) -> None:
    backend = _StubBackend(
        claims=[_claim()], task=_task("WT-1", required=("test output",)), evidence=None
    )
    _use(monkeypatch, backend, tmp_path)
    r = runner.invoke(app, ["gate-check", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 2
    assert "anvil submit WT-1" in r.stdout  # human-readable instruction on stdout


def test_human_continue_prints_continue_message(tmp_path, monkeypatch) -> None:
    _use(monkeypatch, _StubBackend(claims=[]), tmp_path)
    r = runner.invoke(app, ["gate-check", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    assert "finalization may proceed" in r.stdout


def test_blocks_when_required_proofs_present_but_no_evidence(
    tmp_path, monkeypatch
) -> None:
    """E13 hardening: a task with typed required_proofs and NO evidence row must
    fail closed at the stop-gate/finish-gate — checking only the legacy
    required_evidence list (empty for planner tasks) would let it finalize."""
    from anvil.state.models import ProofKind, ProofRequirement

    req = ProofRequirement(
        kind=ProofKind.command, command="uv run pytest -q", label="tests pass"
    )
    backend = _StubBackend(
        claims=[_claim()],
        task=_task("WT-1", required_proofs=(req,)),
        evidence=None,  # nothing submitted
    )
    _use(monkeypatch, backend, tmp_path)
    r = runner.invoke(
        app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False
    )
    assert r.exit_code == 2  # gate-check exits 2 when it blocks
    data = json.loads(r.stdout)["data"]
    assert data["block"] is True
    assert "tests pass" in data["evidence_gate"]["missing"]


def test_actor_falls_back_to_stable_runner_id_when_no_env(
    tmp_path, monkeypatch
) -> None:
    """B47: with no explicit actor and no $ANVIL_ACTOR/$ANVIL_GATE_ACTOR/$USER,
    the actor resolves to a STABLE per-runner signing-key fingerprint (16 hex),
    not the literal 'agent' — so headless runners don't all collide on 'agent'.
    (ANVIL_KEYS_DIR is redirected to a temp dir by the autouse conftest fixture.)
    """
    for var in ("USER", "ANVIL_ACTOR", "ANVIL_GATE_ACTOR",
                "ANVIL_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    backend = _StubBackend(
        claims=[], task=_task("WT-1", required=("test output",)), evidence=None
    )
    _use(monkeypatch, backend, tmp_path)
    r = runner.invoke(app, ["gate-check", "--json"], catch_exceptions=False)  # no --actor
    data = json.loads(r.stdout)["data"]
    actor = data["actor"]
    assert actor not in ("", "agent")
    assert len(actor) == 16
    assert all(c in "0123456789abcdef" for c in actor)
    # No claims for the resolved actor -> nothing to gate -> continue (exit 0).
    assert data["block"] is False
    assert r.exit_code == 0


def test_actor_defaults_to_user_when_set(tmp_path, monkeypatch) -> None:
    # No session env, so the derived identity stays the bare $USER (B47/#103
    # appends a session discriminator only when a session id is present).
    monkeypatch.delenv("ANVIL_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("USER", "alice")
    backend = _StubBackend(
        claims=[_claim(actor="alice")],
        task=_task("WT-1", required=("test output",)), evidence=None,
    )
    _use(monkeypatch, backend, tmp_path)
    r = runner.invoke(app, ["gate-check", "--json"], catch_exceptions=False)  # no --actor
    data = json.loads(r.stdout)["data"]
    assert data["actor"] == "alice"
    assert data["block"] is True


def test_json_output_is_exactly_one_line(tmp_path, monkeypatch) -> None:
    """The OpenClaw plugin's parseEnvelope relies on the envelope being one line."""
    backend = _StubBackend(
        claims=[_claim()], task=_task("WT-1", required=("test output",)), evidence=None
    )
    _use(monkeypatch, backend, tmp_path)
    r = runner.invoke(app, ["gate-check", "--json", "--actor", "agent"], catch_exceptions=False)
    out = r.stdout.strip()
    assert "\n" not in out
    assert json.loads(out)["ok"] is True
