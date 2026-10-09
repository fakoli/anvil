"""Actual registered packet attempt reads never initialize, reap, or write."""
import json

import pytest
from typer.testing import CliRunner

from anvil.attempt_view import read_attempt_view
from anvil.cli import app
from anvil.cli._helpers import _open_backend
from tests.test_profile_cli import _claim_args, _prepared
from tests.test_profile_planning_cli import _invoke

runner = CliRunner()


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("workspace", [False, True])
def test_packet_attempt_is_same_native_read_without_packet_side_effects(tmp_path, monkeypatch, bundle, workspace):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root, state, _ = _prepared(tmp_path, monkeypatch, bundle=bundle, workspace=workspace)
    _invoke(root, _claim_args(bundle, "--shared-tree"))
    target = "B1" if bundle else "T001"
    args = ["packet", target, "--attempt", "--format", "json", *(["--bundle"] if bundle else [])]
    backend = _open_backend(state, project_root=root)
    before = {p.name: p.read_bytes() for p in state.iterdir() if p.is_file() and p.suffix != ".db-shm"}
    packets = list((state / "packets").iterdir())
    try:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("ANVIL_ROOT", str(elsewhere))
        monkeypatch.setattr("anvil.cli.packet_apply._open_backend", lambda *a, **kw: pytest.fail("read opened mutable backend"))
        monkeypatch.setattr("anvil.cli.packet_apply._reap_stale_claims", lambda *a: pytest.fail("read reaped claims"))
        result = _invoke(root, args)
        assert json.loads(result.output) == read_attempt_view(state, target, bundle=bundle)
        assert {p.name: p.read_bytes() for p in state.iterdir() if p.is_file() and p.suffix != ".db-shm"} == before
        assert list((state / "packets").iterdir()) == packets
    finally:
        backend.close()


def test_packet_attempt_unavailable_state_refuses_without_creation(tmp_path):
    result = runner.invoke(app, ["packet", "T001", "--attempt", "--format", "json", "--cwd", str(tmp_path)], catch_exceptions=False)
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "state_unavailable"
    assert list(tmp_path.iterdir()) == []
