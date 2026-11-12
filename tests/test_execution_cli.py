from __future__ import annotations

import json

from typer.testing import CliRunner

from anvil.cli import app
from tests.test_execution import execution_project as execution_project


def test_continue_refuses_before_state_access(tmp_path):
    result = CliRunner().invoke(app, ["continue", "unknown", "--preview", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "continuation_unsupported"
    assert not list(tmp_path.iterdir())


def test_run_preview_json_and_confirmation_options(execution_project):
    _, repo, _, _ = execution_project
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "release:T001",
            "--preview",
            "--cwd",
            str(repo),
            "--actor",
            "synthetic-owner",
            "--thinking",
            "low",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    preview = json.loads(result.output)["data"]
    assert preview["options"] == {"thinking": "low"}
    refused = runner.invoke(
        app,
        [
            "run",
            "release:T001",
            "--confirm",
            preview["preview_id"],
            "--thinking",
            "high",
            "--cwd",
            str(repo),
            "--json",
        ],
    )
    assert refused.exit_code == 1
    assert json.loads(refused.output)["error"]["code"] == "bad_request"


def test_run_configure_preserves_owner_config_and_refuses_replacement(tmp_path, monkeypatch):
    from anvil.config import load_execution_adapter

    runner = CliRunner()

    script = tmp_path / "plugin" / "scripts" / "coordinate.py"
    script.parent.mkdir(parents=True)
    script.write_text("# trusted test executable\n")
    extension = script.parent.parent / "extensions" / "coordination.js"
    extension.parent.mkdir()
    extension.write_text("// extension\n")
    profile = tmp_path / "profile.json"
    profile.write_text("{}")
    global_path = tmp_path / "config" / "config.yaml"
    global_path.parent.mkdir(mode=0o700)
    original = b"# owner comment\nllm_model: preserved\n"
    global_path.write_bytes(original)
    for path in (script, extension, profile, global_path):
        path.chmod(0o600)
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))
    arguments = [
        "run-configure",
        "--script",
        str(script),
        "--interpreter",
        str(script),
        "--profile",
        str(profile),
        "--json",
    ]
    dry = runner.invoke(app, arguments)
    assert dry.exit_code == 0, dry.output
    assert global_path.read_bytes() == original
    written = runner.invoke(app, [*arguments, "--write"])
    assert written.exit_code == 0, written.output
    assert global_path.read_bytes().startswith(original)
    assert load_execution_adapter().script == str(script)
    saved = global_path.read_bytes()
    noop = runner.invoke(app, [*arguments, "--write"])
    assert noop.exit_code == 0 and not json.loads(noop.output)["data"]["changed"]
    assert global_path.read_bytes() == saved
    profile.write_text('{"different":true}')
    refused = runner.invoke(app, [*arguments, "--write"])
    assert refused.exit_code == 1
    assert global_path.read_bytes() == saved
    assert [path.read_bytes() for path in global_path.parent.glob("*.before-execution-*")] == [
        original
    ]
