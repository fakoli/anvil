"""Actual CLI consumers preserve reviewed repository profile contracts."""
import json

import pytest
from typer.testing import CliRunner

from anvil.cli import app
from anvil.cli._helpers import _open_backend, _resolve_state_dir, prd_source_path
from anvil.verification_profiles import MANIFEST
from tests.test_claims import _make_git_repo
from tests.test_profile_planning import _MANIFEST, _field, _markdown

runner = CliRunner()


def _invoke(root, args, *, expected=0, explicit=True):
    result = runner.invoke(
        app, [*args, *(["--cwd", str(root)] if explicit else [])], catch_exceptions=False,
    )
    assert result.exit_code == expected, result.output
    return result


def _project(tmp_path, monkeypatch, prd, *, workspace=False, profiled=True):
    monkeypatch.setenv("ANVIL_STATE_LAYOUT", "workspace" if workspace else "local")
    root = _make_git_repo((tmp_path / "repo").resolve())
    with monkeypatch.context() as context:
        context.chdir(root)
        _invoke(root, ["init", "--name", "Profiles"], explicit=False)
    state = _resolve_state_dir(root)
    source = prd_source_path(state, prd)
    source.parent.mkdir(exist_ok=True)
    (root / MANIFEST).write_text(_MANIFEST)
    (root / "verify.py").write_text("assert True\n")
    source.write_text(_markdown(_field(root) if profiled else ""))
    return root, state, source


def _approve(root, prd):
    extra = [] if prd == "default" else ["--prd", prd]
    _invoke(root, ["prd", "review", *extra, "--reviewer", "reviewer"])
    _invoke(root, ["prd", "review", *extra, "--reviewer", "reviewer", "--approve"])


@pytest.mark.parametrize("prd", ["default", "release"])
@pytest.mark.parametrize("workspace", [False, True])
def test_parse_approve_plan_preserves_frozen_verification(tmp_path, monkeypatch, prd, workspace):
    root, state, source = _project(tmp_path, monkeypatch, prd, workspace=workspace)
    extra = [] if prd == "default" else ["--prd", prd]
    _invoke(root, ["prd", "parse", *extra, "--json"])
    task_id = "T001" if prd == "default" else "release:T001"
    backend = _open_backend(state)
    try:
        binding = backend.get_prd(prd).profile_bindings[task_id]
        assert backend.get_task(task_id) is None
    finally:
        backend.close()
    _approve(root, prd)
    _invoke(root, ["plan", *extra, "--no-llm", "--json"])
    backend = _open_backend(state)
    try:
        task = backend.get_task(task_id)
        assert task.verification.profile_binding == binding
        assert task.verification.commands == ["pytest -q", "python verify.py full"]
        assert [proof.command for proof in task.verification.required_proofs] == task.verification.commands
        assert backend.get_prd(prd).status.value == "approved"
    finally:
        backend.close()


@pytest.mark.parametrize("consumer", ["parse", "plan"])
@pytest.mark.parametrize("drift", ["manifest", "runner", "missing_runner"])
def test_cli_refuses_approved_profile_drift_without_events(tmp_path, monkeypatch, consumer, drift):
    root, state, source = _project(tmp_path, monkeypatch, "default")
    _invoke(root, ["prd", "parse", "--json"])
    _approve(root, "default")
    before = (state / "events.jsonl").read_bytes()
    if drift == "manifest":
        (root / MANIFEST).write_text(_MANIFEST + "\n# Changed source.\n")
    elif drift == "runner":
        (root / "verify.py").write_text("assert False\n")
    else:
        (root / "verify.py").unlink()
    args = ["prd", "parse", "--json"] if consumer == "parse" else ["plan", "--no-llm", "--json"]
    result = _invoke(root, args, expected=1)
    assert json.loads(result.output)["error"]["code"] in {"invalid_revision", "invalid_prd_revision"}
    assert (state / "events.jsonl").read_bytes() == before
    backend = _open_backend(state)
    try:
        assert backend.get_prd().status.value == "approved"
        assert backend.get_task("T001") is None
    finally:
        backend.close()


@pytest.mark.parametrize("explicit", [False, True])
def test_actual_selected_checkout_wins_over_unrelated_cwd_or_env(tmp_path, monkeypatch, explicit):
    root, state, source = _project(tmp_path, monkeypatch, "default")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("ANVIL_ROOT", str(elsewhere if explicit else root))
    _invoke(root, ["prd", "parse", "--json"], explicit=explicit)
    _invoke(root, ["plan", "--no-llm", "--json"], explicit=explicit)
    backend = _open_backend(state)
    try:
        assert backend.get_task("T001").verification.profile_binding is not None
    finally:
        backend.close()


def test_legacy_cli_never_resolves_profile_files(tmp_path, monkeypatch):
    root, state, source = _project(tmp_path, monkeypatch, "default", profiled=False)
    (root / MANIFEST).unlink()
    (root / "verify.py").unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("legacy CLI touched profile files")

    monkeypatch.setattr("anvil.verification_profiles.resolve_profile", forbidden)
    _invoke(root, ["prd", "parse", "--json"])
    _invoke(root, ["plan", "--no-llm", "--json"])
    backend = _open_backend(state)
    try:
        assert backend.get_task("T001").verification.commands == ["pytest -q"]
        assert backend.get_prd().profile_bindings is None
    finally:
        backend.close()
