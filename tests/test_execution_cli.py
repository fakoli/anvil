from __future__ import annotations

import json

import pytest
import yaml
from typer.testing import CliRunner

from anvil.cli import app
from tests.test_config import execution_permission_root as execution_permission_root
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


@pytest.mark.parametrize(
    "original",
    [
        b"# owner comment\nllm_model: preserved\n",
        b"{llm_model: preserved}\n",
        b"---\nllm_model: preserved\n...\n",
    ],
)
def test_run_configure_preserves_owner_config_and_refuses_replacement(
    tmp_path, monkeypatch, original
):
    from anvil.config import load_execution_adapter

    runner = CliRunner()

    script = tmp_path / "plugin" / "scripts" / "coordinate.py"
    script.parent.mkdir(parents=True)
    script.write_text("# trusted test executable\n")
    extension = script.parent.parent / "extensions" / "coordination.js"
    extension.parent.mkdir()
    extension.write_text("// extension\n")
    package = script.parent.parent / "package.json"
    package.write_text('{"name":"codex-pi-coordination","version":"0.2.0"}')
    package.chmod(0o600)
    profile = tmp_path / "profile.json"
    profile.write_text("{}")
    global_path = tmp_path / "config" / "config.yaml"
    global_path.parent.mkdir(mode=0o700)
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
    assert yaml.safe_load(global_path.read_bytes())["llm_model"] == "preserved"
    if original.startswith(b"#"):
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
    assert [
        path.read_bytes() for path in global_path.parent.glob("*.before-execution-*")
    ] == [original]


@pytest.mark.parametrize("failure_stage", [None, "before", "after"])
def test_run_configure_missing_config_checks_ancestry(execution_permission_root, monkeypatch, failure_stage):
    import anvil.cli.execute as execute_cli
    from anvil.config import load_execution_adapter

    plugin = execution_permission_root / "plugin"
    plugin.mkdir(mode=0o700)
    script = plugin / "scripts" / "coordinate.py"
    extension = plugin / "extensions" / "coordination.js"
    profile = plugin / "profile.json"
    package = plugin / "package.json"
    for path in (script, extension, profile, package):
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"{}")
        path.chmod(0o600)
    package.write_text('{"name":"codex-pi-coordination","version":"0.3.0"}')
    outer = execution_permission_root / "owner"
    outer.mkdir()
    outer.chmod(0o777 if failure_stage == "before" else 0o755 if failure_stage else 0o700)
    config = outer / "new" / "private" / "config.yaml"
    if failure_stage == "after":
        original_directory = execute_cli.private_directory

        def create_unsafe_intermediate(path):
            result = original_directory(path)
            (outer / "new").chmod(0o777)
            return result

        monkeypatch.setattr(execute_cli, "private_directory", create_unsafe_intermediate)
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(config))
    arguments = ["run-configure", "--script", str(script), "--interpreter", str(script),
                 "--profile", str(profile), "--json"]
    runner = CliRunner()
    preview = runner.invoke(app, arguments)
    assert preview.exit_code == (1 if failure_stage == "before" else 0), preview.output
    assert not config.parent.exists()
    written = runner.invoke(app, [*arguments, "--write"])
    assert written.exit_code == (1 if failure_stage else 0), written.output
    if failure_stage:
        assert not config.exists()
        assert not list(outer.rglob("*.execution*"))
        if failure_stage == "before":
            assert not list(outer.iterdir())
        else:
            assert not list(config.parent.iterdir())
        assert json.loads(written.output)["error"]["code"] == "invalid_registration"
    else:
        assert load_execution_adapter().script == str(script)
        saved = config.read_bytes()
        noop = runner.invoke(app, [*arguments, "--write"])
        assert noop.exit_code == 0, noop.output
        assert not json.loads(noop.output)["data"]["changed"]
        assert config.read_bytes() == saved
        assert sorted(path.name for path in config.parent.iterdir()) == ["config.yaml"]


@pytest.mark.parametrize(
    "options, model, thinking",
    [
        ([], "configured default", "configured default"),
        (
            ["--provider", "synthetic", "--model", "model", "--thinking", "low"],
            "synthetic/model (explicit)",
            "low (explicit)",
        ),
    ],
)
def test_text_preview_exposes_reviewable_scope(
    execution_project, options, model, thinking
):
    from anvil.execution import git

    backend, repo, _, _ = execution_project
    result = CliRunner().invoke(
        app,
        [
            "run",
            "release:T001",
            "--preview",
            "--cwd",
            str(repo),
            "--actor",
            "synthetic-owner",
            *options,
        ],
    )
    assert result.exit_code == 0, result.output
    for expected in (
        "release:T001",
        "synthetic-owner",
        git(repo, "rev-parse", "HEAD").decode().strip(),
        "README.md",
        backend.get_task("release:T001").verification.commands[0],
        "600 seconds",
        "180 seconds",
        model,
        thinking,
    ):
        assert expected in result.output
    assert not backend.list_active_claims()


@pytest.mark.parametrize("command", ["configure", "run", "capabilities"])
def test_admission_repair_malformed_owner_yaml_is_structured(execution_project, command):
    from anvil.config import global_config_path

    backend, repo, state, registration = execution_project
    config = global_config_path()
    original = b"execution_adapter: [synthetic-private-marker\n"
    config.write_bytes(original)
    before = (state / "events.jsonl").read_bytes()
    if command == "configure":
        arguments = [
            "run-configure", "--script", registration.script,
            "--interpreter", registration.interpreter, "--profile", registration.profile,
        ]
    elif command == "run":
        arguments = ["run", "release:T001", "--preview", "--cwd", str(repo)]
    else:
        arguments = ["run-capabilities", "--cwd", str(repo)]
    result = CliRunner().invoke(app, [*arguments, "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["ok"] is False
    assert "synthetic-private-marker" not in result.output
    assert config.read_bytes() == original
    assert not backend.list_active_claims()
    assert (state / "events.jsonl").read_bytes() == before
