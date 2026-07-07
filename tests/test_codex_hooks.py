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


def test_dispatch_check_claim_extracts_payload(tmp_path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(hooks_mod, "_has_any_anvil_state", lambda cwd: True)

    def _fake_check_claim(*, file, actor, cwd):  # noqa: ANN001, ANN202,A002
        calls.append({"file": file, "actor": actor, "cwd": cwd})

    monkeypatch.setattr(hooks_mod, "hook_check_claim", _fake_check_claim)
    payload = {
        "tool_name": "Edit",
        "tool_input": {"path": "src/app.py"},
        "session_id": "sess-1",
    }
    r = runner.invoke(
        app,
        ["hook", "dispatch", "check-claim", "--cwd", str(tmp_path)],
        input=json.dumps(payload),
        catch_exceptions=False,
    )
    assert r.exit_code == 0
    assert calls == [{"file": "src/app.py", "actor": "sess-1", "cwd": tmp_path}]


def test_dispatch_uses_payload_cwd_when_flag_omitted(tmp_path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(hooks_mod, "_has_any_anvil_state", lambda cwd: True)

    def _fake_record_file_change(*, file, tool, actor, cwd):  # noqa: ANN001, ANN202,A002
        calls.append({"file": file, "tool": tool, "actor": actor, "cwd": cwd})

    monkeypatch.setattr(hooks_mod, "hook_record_file_change", _fake_record_file_change)
    payload = {
        "cwd": str(tmp_path),
        "tool_name": "Write",
        "tool_input": {"path": "src/from-payload.py"},
        "session_id": "sess-payload",
    }
    r = runner.invoke(
        app,
        ["hook", "dispatch", "record-file-change"],
        input=json.dumps(payload),
        catch_exceptions=False,
    )
    assert r.exit_code == 0
    assert calls == [
        {
            "file": "src/from-payload.py",
            "tool": "Write",
            "actor": "sess-payload",
            "cwd": tmp_path,
        }
    ]


def test_dispatch_fast_paths_without_state(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(hooks_mod, "_has_any_anvil_state", lambda cwd: False)
    monkeypatch.setattr(
        hooks_mod,
        "hook_record_file_change",
        lambda **kwargs: calls.append("record"),
    )
    payload = {
        "tool_name": "Write",
        "tool_input": {"path": "src/app.py"},
        "session_id": "sess-1",
    }
    r = runner.invoke(
        app,
        ["hook", "dispatch", "record-file-change", "--cwd", str(tmp_path)],
        input=json.dumps(payload),
        catch_exceptions=False,
    )
    assert r.exit_code == 0
    assert calls == []


def test_dispatch_capture_evidence_extracts_payload_and_temp_files(
    tmp_path, monkeypatch
) -> None:
    calls: list[dict[str, object]] = []
    temp_paths: list[Path] = []
    monkeypatch.setattr(hooks_mod, "_has_any_anvil_state", lambda cwd: True)

    def _fake_capture(
        *,
        command,
        exit_code,
        stdout_file,
        stderr_file,
        actor,
        cwd,
    ):  # noqa: ANN001, ANN202
        temp_paths.extend([stdout_file, stderr_file])
        calls.append(
            {
                "command": command,
                "exit_code": exit_code,
                "stdout": stdout_file.read_text(encoding="utf-8"),
                "stderr": stderr_file.read_text(encoding="utf-8"),
                "actor": actor,
                "cwd": cwd,
            }
        )

    monkeypatch.setattr(hooks_mod, "hook_capture_evidence", _fake_capture)
    payload = {
        "tool_input": {"command": "pytest -q"},
        "tool_response": {"stdout": "ok", "stderr": "warn", "exit_code": 1},
        "session_id": "sess-2",
    }
    r = runner.invoke(
        app,
        ["hook", "dispatch", "capture-evidence", "--cwd", str(tmp_path)],
        input=json.dumps(payload),
        catch_exceptions=False,
    )
    assert r.exit_code == 0
    assert calls == [
        {
            "command": "pytest -q",
            "exit_code": 1,
            "stdout": "ok",
            "stderr": "warn",
            "actor": "sess-2",
            "cwd": tmp_path,
        }
    ]
    assert temp_paths and all(not p.exists() for p in temp_paths)


def test_dispatch_capture_evidence_ignores_non_verification_command(
    tmp_path, monkeypatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(hooks_mod, "_has_any_anvil_state", lambda cwd: True)
    monkeypatch.setattr(
        hooks_mod,
        "hook_capture_evidence",
        lambda **kwargs: calls.append("capture"),
    )
    payload = {
        "tool_input": {"command": "echo hello"},
        "tool_response": {"stdout": "hello", "stderr": "", "exit_code": 0},
        "session_id": "sess-2",
    }
    r = runner.invoke(
        app,
        ["hook", "dispatch", "capture-evidence", "--cwd", str(tmp_path)],
        input=json.dumps(payload),
        catch_exceptions=False,
    )
    assert r.exit_code == 0
    assert calls == []


def test_dispatch_detect_state_renders_status_banner(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hooks_mod, "_language_for_cwd", lambda cwd: "Python")
    monkeypatch.setattr(
        hooks_mod,
        "_status_hook_line",
        lambda cwd: ("active-claims:1 ready-tasks:2 blockers:0 prd-status:approved", 0),
    )
    r = runner.invoke(
        app,
        ["hook", "dispatch", "detect-state", "--cwd", str(tmp_path)],
        input="{}",
        catch_exceptions=False,
    )
    assert r.exit_code == 0
    payload = json.loads(r.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert payload["hookSpecificOutput"]["additionalContext"] == (
        "[anvil] Language: Python | "
        "active-claims:1 ready-tasks:2 blockers:0 prd-status:approved"
    )


def test_dispatch_heartbeat_preserves_default_actor_resolution(tmp_path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(hooks_mod, "_has_any_anvil_state", lambda cwd: True)

    def _fake_heartbeat(*, actor, cwd):  # noqa: ANN001, ANN202
        calls.append({"actor": actor, "cwd": cwd})

    monkeypatch.setattr(hooks_mod, "hook_heartbeat", _fake_heartbeat)
    r = runner.invoke(
        app,
        ["hook", "dispatch", "heartbeat", "--cwd", str(tmp_path)],
        input='{"session_id":"ignored-for-heartbeat"}',
        catch_exceptions=False,
    )
    assert r.exit_code == 0
    assert calls == [{"actor": None, "cwd": tmp_path}]


def _claim(actor: str = "agent", cid: str = "C1", task_id: str = "WT-1") -> object:
    return types.SimpleNamespace(claimed_by=actor, id=cid, task_id=task_id, expected_files=[])


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


def test_heartbeat_only_renews_own_actor_claims(
    approved_backend, frozen_clock, tmp_path, monkeypatch
) -> None:
    """Heartbeat renews ONLY the actor's own claims — never another actor's lease
    (pins the claimed_by filter; without it a heartbeat would defeat lease handoff)."""
    from anvil.claims.manager import ClaimManager
    from anvil.workflows.tasks import create_workflow_task

    t_agent = create_workflow_task(
        approved_backend, title="a", description="d", actor="agent", clock=frozen_clock,
    )
    t_other = create_workflow_task(
        approved_backend, title="b", description="d", actor="other", clock=frozen_clock,
    )
    agent_claim = ClaimManager(approved_backend, frozen_clock, actor="agent").claim(t_agent).claim
    other_claim = ClaimManager(approved_backend, frozen_clock, actor="other").claim(t_other).claim

    renewed: list[str] = []
    orig_renew = ClaimManager.renew

    def _spy(self, claim_id):  # noqa: ANN001, ANN202
        renewed.append(claim_id)
        return orig_renew(self, claim_id)

    monkeypatch.setattr("anvil.claims.manager.ClaimManager.renew", _spy)
    _use_backend(monkeypatch, _NoCloseProxy(approved_backend), tmp_path)
    r = runner.invoke(app, ["hook", "heartbeat", "--actor", "agent"], catch_exceptions=False)
    assert r.exit_code == 0
    assert agent_claim.id in renewed
    assert other_claim.id not in renewed  # NEVER renew another actor's lease
