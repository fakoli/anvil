from __future__ import annotations

import json

import pytest
import yaml
from typer.testing import CliRunner

from anvil.cli import app
from tests.test_config import execution_permission_root as execution_permission_root
from tests.test_config import execution_upgrade_owner as execution_upgrade_owner
from tests.test_execution import execution_project as execution_project


def test_continue_refuses_ephemeral_registration(execution_project):
    _, repo, state, _ = execution_project
    original = (state / "events.jsonl").read_bytes()
    result = CliRunner().invoke(app, ["continue", "a" * 32, "--preview", "--cwd", str(repo), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "continuation_unsupported"
    assert (state / "events.jsonl").read_bytes() == original


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
    import anvil.execution as execution
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
        original_directory = execution.private_directory

        def create_unsafe_intermediate(path):
            result = original_directory(path)
            (outer / "new").chmod(0o777)
            return result

        monkeypatch.setattr(execution, "private_directory", create_unsafe_intermediate)
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


@pytest.mark.parametrize("selected", [[], ["--task", "synthetic:T002", "--provider", "synthetic", "--model", "model", "--thinking", "off", "--wall-seconds", "120"]])
def test_continue_cli_preview_and_confirmation_bind_saved_route(tmp_path, monkeypatch, selected):
    import anvil.cli.execute as execute_cli

    prior_run = "a" * 32
    preview_id = "b" * 32
    dispatched = "c" * 32
    calls = []

    def preview(cwd, run_id, actor, options, wall_seconds=600, task_id=None):
        calls.append((cwd, run_id, actor, options.data(), wall_seconds, task_id))
        return {"preview_id": preview_id, "expires_at": "synthetic-expiry",
                "task_id": task_id or "synthetic:T001", "actor": actor,
                "source_sha": "d" * 40, "wall_seconds": wall_seconds,
                "options": options.data(), "writable_paths": ["task_labels.py"],
                "commands": ["python verify.py"]}

    def confirm(cwd, run_id, actor, saved_preview):
        assert (cwd, run_id, actor, saved_preview) == (tmp_path, prior_run, "synthetic-owner", preview_id)
        return {"run_id": dispatched}

    monkeypatch.setattr(execute_cli, "preview_continue", preview)
    monkeypatch.setattr(execute_cli, "confirm_continue", confirm)
    runner = CliRunner()
    result = runner.invoke(app, ["continue", prior_run, "--preview", "--cwd", str(tmp_path),
                                 "--actor", "synthetic-owner", *selected])
    assert result.exit_code == 0, result.output
    assert f"Continue from: {prior_run}" in result.output
    assert f"anvil continue {prior_run} --confirm {preview_id}" in result.output
    assert "task_labels.py" in result.output and "python verify.py" in result.output
    assert calls == [(tmp_path, prior_run, "synthetic-owner",
                      {"provider": "synthetic", "model": "model", "thinking": "off"} if selected else {},
                      120 if selected else 600, "synthetic:T002" if selected else None)]
    confirmed = runner.invoke(app, ["continue", prior_run, "--confirm", preview_id,
                                    "--cwd", str(tmp_path), "--actor", "synthetic-owner", "--json"])
    assert confirmed.exit_code == 0, confirmed.output
    assert json.loads(confirmed.output)["data"]["run_id"] == dispatched


@pytest.mark.parametrize("extra", [["--task", "synthetic:T002"], ["--thinking", "low"], ["--provider", "synthetic"], ["--model", "model"], ["--wall-seconds", "120"], ["--preview"]])
def test_continue_confirmation_rejects_new_options_before_admission(monkeypatch, extra):
    import anvil.cli.execute as execute_cli

    monkeypatch.setattr(execute_cli, "_resolve_project_dir", lambda *_: pytest.fail("No admission"))
    result = CliRunner().invoke(app, ["continue", "a" * 32, "--confirm", "b" * 32, *extra, "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "bad_request"


@pytest.mark.parametrize("arguments", [[], ["a" * 32, "--preview", "b" * 32], ["a" * 32, "--actor", "synthetic"]])
def test_run_status_route_ambiguity_refuses_before_read(monkeypatch, arguments):
    import anvil.cli.execute as execute_cli

    monkeypatch.setattr(execute_cli, "_resolve_project_dir", lambda *_: pytest.fail("No read"))
    result = CliRunner().invoke(app, ["run-status", *arguments, "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "bad_request"


def test_run_status_exact_preview_readback_is_an_existing_short_command(tmp_path, monkeypatch):
    import anvil.cli.execute as execute_cli

    preview_id, run_id = "a" * 32, "b" * 32

    def inspect(cwd, preview, actor):
        assert (cwd, preview, actor) == (tmp_path, preview_id, "synthetic-owner")
        return {"state": "consumed", "run_id": run_id}

    monkeypatch.setattr(execute_cli, "inspect_execution_preview", inspect)
    result = CliRunner().invoke(app, ["run-status", "--preview", preview_id,
                                     "--actor", "synthetic-owner", "--cwd", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "consumed" in result.output and f"anvil run-status {run_id}" in result.output


@pytest.mark.parametrize("extra", [["--provider", "synthetic"], ["--model", "model"], ["--provider", "synthetic", "--model", "model"]])
def test_capability_model_selection_requires_pair_and_refresh(monkeypatch, extra):
    import anvil.cli.execute as execute_cli

    monkeypatch.setattr(execute_cli, "_resolve_project_dir", lambda *_: pytest.fail("No metadata launch"))
    result = CliRunner().invoke(app, ["run-capabilities", *extra, "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] in {"unsupported_options", "bad_request"}


@pytest.mark.parametrize("selected", [False, True])
def test_capability_refresh_forwards_only_exact_optional_pair(tmp_path, monkeypatch, selected):
    import anvil.cli.execute as execute_cli

    def refresh(cwd, provider=None, model=None):
        assert cwd == tmp_path
        assert (provider, model) == (("synthetic", "model") if selected else (None, None))
        return {"run_id": "a" * 32}

    monkeypatch.setattr(execute_cli, "refresh_execution_capabilities", refresh)
    result = CliRunner().invoke(app, ["run-capabilities", "--refresh", "--cwd", str(tmp_path),
                                     *(["--provider", "synthetic", "--model", "model"] if selected else []),
                                     "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["run_id"] == "a" * 32


def test_short_upgrade_cli_displays_preimages_and_confirms(execution_upgrade_owner):
    owner = execution_upgrade_owner
    runner = CliRunner()
    preview = runner.invoke(app, ["run-configure", "--upgrade", "--preview", "--json"])
    assert preview.exit_code == 0, preview.output
    preview_id = json.loads(preview.output)["data"]["preview_id"]
    assert owner.config.read_bytes() == owner.original
    confirmation = runner.invoke(app, ["run-configure", "--upgrade", "--confirm", preview_id, "--json"])
    assert confirmation.exit_code == 0, confirmation.output
    assert json.loads(confirmation.output)["data"]["written"]
    text = runner.invoke(app, ["run-configure", "--upgrade", "--preview"])
    assert text.exit_code == 0, text.output
    for expected in ("Expected registration:", "Configuration bytes:", "Interpreter:",
                     "Script:", "Profile:", "Extension sha256:", "anvil run-configure --upgrade --confirm"):
        assert expected in text.output


@pytest.mark.parametrize("arguments", [
    ["--upgrade", "--write"], ["--upgrade"], ["--preview"],
    ["--upgrade", "--preview", "--confirm", "a" * 32],
    ["--upgrade", "--confirm", "a" * 32, "--script", "/synthetic/script"],
    ["--upgrade", "--confirm", "a" * 32, "--profile", "/synthetic/profile"],
    ["--upgrade", "--confirm", "a" * 32, "--interpreter", "/synthetic/python"],
])
def test_upgrade_cli_cannot_bypass_frozen_candidate(monkeypatch, arguments):
    import anvil.cli.execute as execute_cli

    monkeypatch.setattr(execute_cli, "preview_execution_adapter_upgrade", lambda *_: pytest.fail("No preview"))
    monkeypatch.setattr(execute_cli, "confirm_execution_adapter_upgrade", lambda *_: pytest.fail("No confirmation"))
    result = CliRunner().invoke(app, ["run-configure", *arguments, "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "invalid_registration"
