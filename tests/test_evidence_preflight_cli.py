"""Typer preflight adapter; final root-app registration is qualified by T011/T009."""
import json

import pytest
import typer
from typer.testing import CliRunner

from anvil.attempt_view import read_evidence_preflight
from anvil.cli.packet_apply import evidence_preflight
from tests.test_evidence_preflight import _capture, _claim

adapter = typer.Typer()
adapter.command("evidence-preflight")(evidence_preflight)
runner = CliRunner()


@pytest.mark.parametrize("failed", [False, True])
def test_preflight_adapter_returns_complete_shared_read_without_mutation(tmp_path, monkeypatch, failed):
    task, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    _capture(tmp_path, failed=failed)
    state = tmp_path / ".anvil"
    log = (state / "events.jsonl").read_bytes()
    buffer = state / ".evidence-buffer" / f"{claim}.json"
    before = buffer.read_bytes()
    monkeypatch.setattr("anvil.cli.packet_apply._open_backend", lambda *a, **kw: pytest.fail("mutable backend"))
    result = runner.invoke(adapter, [task, "--cwd", str(tmp_path), "--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["command"] == "evidence-preflight"
    assert envelope["data"] == read_evidence_preflight(state, task)
    assert (state / "events.jsonl").read_bytes() == log
    assert buffer.read_bytes() == before


def test_preflight_adapter_overflow_is_atomic_refusal(tmp_path, monkeypatch):
    task, claim = _claim(tmp_path)
    monkeypatch.setenv("ANVIL_CLAIM_ID", claim)
    for _ in range(17):
        _capture(tmp_path, failed=True)
    log = (tmp_path / ".anvil" / "events.jsonl").read_bytes()
    result = runner.invoke(adapter, [task, "--cwd", str(tmp_path), "--json"], catch_exceptions=False)
    assert result.exit_code == 1
    refusal = json.loads(result.output)["error"]
    assert refusal["code"] == "invalid_hierarchy"
    assert str(tmp_path) not in json.dumps(refusal)
    assert (tmp_path / ".anvil" / "events.jsonl").read_bytes() == log
