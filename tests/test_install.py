"""Tests for ``anvil install <harness> [--write]`` — the MCP+instruction writer.

``install`` reuses ``mcp-config``'s ``CLIENTS`` envelope for JSON harnesses and
splices anvil's ``AGENTS.md`` into a marked, removable block where each harness
reads it (never a wholesale overwrite). Codex installs natively via its own CLI
(``codex mcp add`` / ``plugin marketplace add``) — anvil never edits config.toml.
Every modified file is backed up + logged so ``--rollback`` is exact. Default is
a safe dry-run; ``--write`` performs the (idempotent) changes.

These drive the command through Typer's ``CliRunner`` (as ``test_mcp_config.py``
does), with ``HOME`` monkeypatched and the project root pinned via ``ANVIL_ROOT``
so writes land under ``tmp_path`` and never touch the real machine.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import anvil.cli.install  # noqa: F401  (ensure submodule is in sys.modules)
from anvil.cli import app
from anvil.cli.install import HARNESSES

# `anvil.cli` re-exports the `install` FUNCTION, shadowing the submodule attribute,
# so `anvil.cli.install` resolves to the function. Grab the real module to patch it.
install_mod = sys.modules["anvil.cli.install"]

runner = CliRunner()


def _assert_checkout_launcher(spec: dict) -> None:
    if spec["command"] == "bash":
        assert spec["args"][-1].endswith("bin/anvil-mcp")
        return

    assert spec["command"] == "uv"
    assert spec["args"][0] == "run"
    assert "--project" in spec["args"]
    assert spec["args"][-3:] == ["python", "-m", "anvil.mcp_server"]


def _assert_uv_checkout_launcher(spec: dict) -> None:
    assert spec["command"] == "uv"
    assert spec["args"][0:2] == ["run", "--quiet"]
    assert "--project" in spec["args"]
    assert spec["args"][-3:] == ["python", "-m", "anvil.mcp_server"]


def _assert_checkout_argv(argv: list[str]) -> None:
    _assert_checkout_launcher({"command": argv[0], "args": argv[1:]})


def _openclaw_arg_values(argv: list[str]) -> list[str]:
    values: list[str] = []
    for idx, token in enumerate(argv):
        if token == "--arg":
            values.append(argv[idx + 1])
        elif token.startswith("--arg="):
            values.append(token.removeprefix("--arg="))
    return values


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolate HOME and the project root under tmp_path.

    - HOME → ``tmp_path/home`` (home-scoped writes land here).
    - ANVIL_ROOT → ``tmp_path/project`` (project-scoped writes land here).
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANVIL_ROOT", str(project))

    # Never shell out to a real `codex` CLI in tests — record the commands a native
    # install WOULD run instead, so assertions stay hermetic and side-effect-free.
    ran: list[list[str]] = []
    cwds: list[str | None] = []

    def _fake_run(cmds: list, *, run: bool, cwd: str | None = None) -> list:
        ran.extend(cmds)
        cwds.append(cwd)
        # Simulate pi's REAL persisted behavior so anvil's rollback verification
        # runs against faithful state: install/remove -l mutate
        # <cwd>/.pi/settings.json (packages list); removing an absent package
        # exits non-zero ("No matching package found"), and a successful remove
        # persists the change. codex/openclaw stay inert (global, configless).
        if run and cmds and cmds[0] and cmds[0][0] == "pi":
            base = Path(cwd) if cwd else install_mod._project_root()
            settings = base / ".pi" / "settings.json"
            packages: list[str] = []
            if settings.is_file():
                try:
                    packages = list(json.loads(settings.read_text()).get("packages") or [])
                except (json.JSONDecodeError, OSError):
                    packages = []
            verb = cmds[0][1] if len(cmds[0]) > 1 else ""
            spec = cmds[0][3] if len(cmds[0]) > 3 else ""
            if verb == "install" and spec:
                if spec not in packages:
                    packages.append(spec)
            elif verb == "remove" and spec:
                if spec not in packages:
                    return [
                        {"cmd": " ".join(cmds[0]), "ran": True, "ok": False,
                         "detail": "No matching package found"}
                    ]
                packages = [p for p in packages if p != spec]
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({"packages": packages}))
        return [
            {"cmd": " ".join(c), "ran": run, "ok": True if run else None, "detail": ""}
            for c in cmds
        ]

    monkeypatch.setattr(install_mod, "_run_or_print", _fake_run)
    return {"home": home, "project": project, "native_cmds": ran, "native_cwds": cwds}


def test_known_harnesses_present() -> None:
    """The verified harnesses from the spec are all in the registry."""
    for name in ("codex", "copilot", "gemini", "openclaw", "pi", "cursor",
                 "windsurf", "cline", "zed", "openhands", "opencode", "roo",
                 "amp", "continue", "goose"):
        assert name in HARNESSES


@pytest.mark.parametrize("harness", sorted(HARNESSES))
def test_dry_run_writes_nothing(harness: str, sandbox: dict[str, Path]) -> None:
    """No ``--write`` → dry-run: exit 0, NOTHING written to disk, paths printed."""
    result = runner.invoke(app, ["install", harness], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout + result.stderr

    # Nothing created under either sandbox root.
    home_files = list(sandbox["home"].rglob("*"))
    project_files = list(sandbox["project"].rglob("*"))
    assert [p for p in home_files if p.is_file()] == []
    assert [p for p in project_files if p.is_file()] == []

    # The dry-run surfaces SOMETHING per harness (on stderr — stdout stays clean),
    # by tier: codex previews the AGENTS.md splice; openclaw prints its native
    # commands; every other (MCP-only best-effort) harness surfaces its MCP line.
    h = HARNESSES[harness]
    if h.writes_instructions:
        assert "Instruction file" in result.stderr
    elif h.native_installer:
        assert "Run these" in result.stderr
    else:
        assert "MCP config" in result.stderr


def test_dry_run_json_envelope(sandbox: dict[str, Path]) -> None:
    """`--json` dry-run emits one success envelope listing every action."""
    result = runner.invoke(
        app, ["install", "--json", "codex"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout
    env = json.loads(result.stdout.strip())
    assert env["ok"] is True
    assert env["command"] == "install"
    data = env["data"]
    assert data["harness"] == "codex"
    assert data["write"] is False
    assert set(data["mcp"]) == {"path", "action", "note"}
    assert set(data["instruction"]) == {"path", "action"}


def test_mcp_only_harness_writes_only_mcp_config(sandbox: dict[str, Path]) -> None:
    """cursor is MCP-only (best-effort tier): writes ~/.cursor/mcp.json and NOTHING
    else — no AGENTS.md splice, no .agents/ skills drop."""
    r = runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    assert r.exit_code == 0, r.stdout + r.stderr
    assert (sandbox["home"] / ".cursor" / "mcp.json").is_file()  # MCP written
    assert not (sandbox["project"] / "AGENTS.md").exists()       # no instruction splice
    assert not (sandbox["project"] / ".agents").exists()         # no skills drop
    # The JSON envelope marks the instruction skipped and carries no skills key.
    j = runner.invoke(app, ["install", "--json", "cursor"], catch_exceptions=False)
    data = json.loads(j.stdout.strip())["data"]
    assert data["instruction"]["action"] == "skipped"
    assert "skills" not in data


def test_codex_still_splices_agents_md(sandbox: dict[str, Path]) -> None:
    """codex remains the one supported harness that writes the AGENTS.md block —
    and never drops the (now-removed) neutral .agents/ skills."""
    r = runner.invoke(app, ["install", "codex", "--write"], catch_exceptions=False)
    assert r.exit_code == 0, r.stdout + r.stderr
    text = (sandbox["project"] / "AGENTS.md").read_text()
    assert "BEGIN ANVIL" in text
    assert (_repo_root() / "AGENTS.md").read_text().strip() in text
    assert not (sandbox["project"] / ".agents").exists()


@pytest.mark.parametrize(
    "harness", ["gemini", "openhands", "continue", "goose", "cline"]
)
def test_mcp_only_none_harness_writes_nothing_to_project(
    harness: str, sandbox: dict[str, Path]
) -> None:
    """An MCP-only harness whose MCP ships another way (mcp_merge="none") makes no
    project-tree writes: no MCP config, no AGENTS.md splice, no skills drop."""
    r = runner.invoke(app, ["install", harness, "--write"], catch_exceptions=False)
    assert r.exit_code == 0, r.stdout + r.stderr
    assert [p for p in sandbox["project"].rglob("*") if p.is_file()] == []
    data = json.loads(
        runner.invoke(app, ["install", "--json", harness]).stdout.strip()
    )["data"]
    assert data["mcp"]["action"] == "skipped"
    assert data["instruction"]["action"] == "skipped"


def test_write_json_config_idempotent(sandbox: dict[str, Path]) -> None:
    """`install cursor --write` writes MCP JSON with the reused top key; the
    second write is byte-identical (idempotent)."""
    r1 = runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    assert r1.exit_code == 0, r1.stdout + r1.stderr

    cfg = sandbox["home"] / ".cursor" / "mcp.json"
    assert cfg.is_file()
    data = json.loads(cfg.read_text())
    assert "mcpServers" in data  # reused CLIENTS["cursor"] top key
    assert "anvil" in data["mcpServers"]
    first = cfg.read_text()

    r2 = runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    assert r2.exit_code == 0
    assert cfg.read_text() == first  # idempotent


def test_write_json_preserves_unrelated_server(sandbox: dict[str, Path]) -> None:
    """A pre-existing unrelated server in the target JSON survives the merge."""
    cfg = sandbox["home"] / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))

    result = runner.invoke(
        app, ["install", "cursor", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert "anvil" in data["mcpServers"]


def test_codex_never_touches_config_toml(sandbox: dict[str, Path]) -> None:
    """Codex goes native: anvil must NOT hand-edit ~/.codex/config.toml (the thing
    that corrupted it). A pre-existing config is left byte-for-byte untouched."""
    cfg = sandbox["home"] / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    original = (
        'model = "gpt-5.5"\n\n'
        "[projects]\n"
        '"/Users/me/code/proj" = { trust_level = "trusted" }\n'
    )
    cfg.write_text(original)

    result = runner.invoke(
        app, ["install", "codex", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert cfg.read_text() == original  # untouched — Codex writes its own config


def test_codex_native_commands_generated(sandbox: dict[str, Path]) -> None:
    """`install codex --write` drives the Codex CLI: marketplace add + mcp add."""
    result = runner.invoke(
        app, ["install", "codex", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    cmds = [" ".join(c) for c in sandbox["native_cmds"]]
    # Marketplace source is the public slug (works for any install method), not a
    # local path that wouldn't resolve from a pip wheel.
    assert "codex plugin marketplace add fakoli/anvil" in cmds
    mcp_add = next(
        c for c in sandbox["native_cmds"] if c[:3] == ["codex", "mcp", "add"]
    )
    cmd_idx = mcp_add.index("--") + 1
    _assert_uv_checkout_launcher({"command": mcp_add[cmd_idx], "args": mcp_add[cmd_idx + 1:]})


def test_openclaw_native_commands_generated(sandbox: dict[str, Path]) -> None:
    """OpenClaw installs via its own CLI: `mcp add` (--no-probe) + `plugins
    install` from anvil's Claude-compatible marketplace."""
    result = runner.invoke(
        app, ["install", "openclaw", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    cmds = [" ".join(c) for c in sandbox["native_cmds"]]
    mcp_add = next(
        c
        for c in sandbox["native_cmds"]
        if c[:4] == ["openclaw", "mcp", "add", "anvil"]
    )
    # --no-probe: a cold-venv probe timeout must not block the save (half-install).
    assert "--no-probe" in mcp_add
    cmd_idx = mcp_add.index("--command") + 1
    arg_values = _openclaw_arg_values(mcp_add)
    _assert_checkout_launcher({"command": mcp_add[cmd_idx], "args": arg_values})
    assert not any(
        v == "--arg" and i + 1 < len(mcp_add) and mcp_add[i + 1].startswith("-")
        for i, v in enumerate(mcp_add)
    ), "hyphen-leading MCP args must use --arg=<value> for OpenClaw"
    # --force: re-install refreshes the plugin instead of a silent "already exists".
    assert "openclaw plugins install anvil --marketplace fakoli/anvil --force" in cmds


def test_openclaw_uv_run_args_are_not_parsed_as_openclaw_flags(
    sandbox: dict[str, Path],
) -> None:
    """The uv launcher contains dash-leading args; emit them as --arg=<value>."""
    result = runner.invoke(
        app, ["install", "openclaw", "--write", "--uv-run"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    mcp_add = next(
        c
        for c in sandbox["native_cmds"]
        if c[:4] == ["openclaw", "mcp", "add", "anvil"]
    )
    assert "--command" in mcp_add
    assert mcp_add[mcp_add.index("--command") + 1] == "uv"
    assert "--arg=--quiet" in mcp_add
    assert "--arg=--project" in mcp_add
    assert "--arg=-m" in mcp_add
    assert not any(
        v == "--arg" and i + 1 < len(mcp_add) and mcp_add[i + 1].startswith("-")
        for i, v in enumerate(mcp_add)
    )
    _assert_checkout_launcher(
        {
            "command": "uv",
            "args": _openclaw_arg_values(mcp_add),
        }
    )


def test_native_command_failure_is_surfaced(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native command that RAN and failed must show a `⚠` with detail, not pass
    silently (review Finding 3 — exit-0-with-error misclassification)."""
    def _failing(cmds: list, *, run: bool, cwd: str | None = None) -> list:
        return [{"cmd": " ".join(c), "ran": True, "ok": False, "detail": "boom"}
                for c in cmds]

    monkeypatch.setattr(install_mod, "_run_or_print", _failing)
    result = runner.invoke(app, ["install", "openclaw", "--write"], catch_exceptions=False)
    assert "⚠" in result.stderr and "boom" in result.stderr


def test_openclaw_touches_no_user_files(sandbox: dict[str, Path]) -> None:
    """OpenClaw owns its own config — anvil must write NO files: no .mcp.json,
    no AGENTS.md, no .agents/skills (the old row hand-edited .mcp.json + AGENTS.md)."""
    runner.invoke(app, ["install", "openclaw", "--write"], catch_exceptions=False)
    assert not (sandbox["project"] / ".mcp.json").exists()
    assert not (sandbox["project"] / "AGENTS.md").exists()
    assert not (sandbox["project"] / ".agents").exists()
    # No backups either — nothing was modified.
    assert list(sandbox["project"].rglob("*.anvil-bak")) == []


def test_openclaw_install_prints_sandbox_note_and_cron_tip(
    sandbox: dict[str, Path],
) -> None:
    """Every openclaw install surfaces the sandbox-allowlist prerequisite and points
    at the opt-in cron recipes (B42 Phase 1)."""
    r = runner.invoke(app, ["install", "openclaw"], catch_exceptions=False)
    assert r.exit_code == 0
    assert "sandbox.tools.allow" in r.stderr  # the prerequisite note
    assert "--cron-recipes" in r.stderr  # discovery tip
    assert "openclaw cron add" not in r.stderr  # recipes only with the opt-in flag


def test_openclaw_cron_recipes_printed_never_run(sandbox: dict[str, Path]) -> None:
    """`--cron-recipes` PRINTS the recipes (incl. notify-digest + queue probe) but
    anvil registers nothing — honoring the OpenClaw no-files contract."""
    r = runner.invoke(
        app, ["install", "openclaw", "--cron-recipes"], catch_exceptions=False
    )
    assert r.exit_code == 0
    assert "openclaw cron add" in r.stderr
    assert "anvil notify-digest" in r.stderr
    assert "anvil next -q" in r.stderr
    # anvil never RAN a cron command — only the mcp/plugin install commands ran.
    ran = [" ".join(c) for c in sandbox["native_cmds"]]
    assert not any("cron add" in c for c in ran)


def test_cron_recipes_rejected_for_non_openclaw(sandbox: dict[str, Path]) -> None:
    """`--cron-recipes` is OpenClaw-only (Gateway cron) — others refuse cleanly."""
    r = runner.invoke(app, ["install", "codex", "--cron-recipes"])
    assert r.exit_code == 2
    assert "OpenClaw-only" in r.stderr


def test_openclaw_install_tips_finish_gate(sandbox: dict[str, Path]) -> None:
    """A plain openclaw install points at the opt-in --finish-gate recipe but does
    NOT print the link recipe (that needs the flag) — B42 Phase 2."""
    r = runner.invoke(app, ["install", "openclaw"], catch_exceptions=False)
    assert r.exit_code == 0
    assert "--finish-gate" in r.stderr  # discovery tip
    assert "plugins install --link" not in r.stderr  # recipe only with the opt-in flag


def test_openclaw_finish_gate_recipe_printed_never_linked(
    sandbox: dict[str, Path],
) -> None:
    """`--finish-gate` PRINTS the plugin install recipe (link + enable +
    allowConversationAccess + restart) but anvil links/registers nothing —
    honoring the OpenClaw no-files contract."""
    r = runner.invoke(
        app, ["install", "openclaw", "--finish-gate"], catch_exceptions=False
    )
    assert r.exit_code == 0
    assert "plugins install --link" in r.stderr
    assert "anvil-finish-gate" in r.stderr
    assert "allowConversationAccess" in r.stderr
    # anvil never RAN a plugin-link command — only the mcp/plugin(server) installs ran.
    ran = [" ".join(c) for c in sandbox["native_cmds"]]
    assert not any("--link" in c for c in ran)


def test_finish_gate_rejected_for_non_openclaw(sandbox: dict[str, Path]) -> None:
    """`--finish-gate` is OpenClaw-only (native plugin hooks) — others refuse cleanly."""
    r = runner.invoke(app, ["install", "codex", "--finish-gate"])
    assert r.exit_code == 2
    assert "OpenClaw-only" in r.stderr


def test_openclaw_rollback_runs_native_removers(sandbox: dict[str, Path]) -> None:
    """OpenClaw rollback undoes via its own removers: `mcp unset` + `plugins
    uninstall`."""
    runner.invoke(app, ["install", "openclaw", "--write"], catch_exceptions=False)
    sandbox["native_cmds"].clear()
    runner.invoke(app, ["install", "openclaw", "--rollback"], catch_exceptions=False)
    cmds = [" ".join(c) for c in sandbox["native_cmds"]]
    assert "openclaw mcp unset anvil" in cmds
    assert "openclaw plugins uninstall anvil --force" in cmds


def test_codex_rollback_runs_native_removers(sandbox: dict[str, Path]) -> None:
    """Codex rollback drives `codex mcp remove` + `marketplace remove` and strips
    our AGENTS.md block."""
    instr = sandbox["project"] / "AGENTS.md"
    instr.write_text("# mine\n")
    runner.invoke(app, ["install", "codex", "--write"], catch_exceptions=False)
    sandbox["native_cmds"].clear()

    runner.invoke(app, ["install", "codex", "--rollback"], catch_exceptions=False)
    cmds = [" ".join(c) for c in sandbox["native_cmds"]]
    assert "codex mcp remove anvil" in cmds
    assert any("marketplace remove" in c for c in cmds)
    assert instr.read_text() == "# mine\n"  # our block stripped, user content kept


def test_codex_rollback_without_install_does_not_touch_global(
    sandbox: dict[str, Path]
) -> None:
    """Rolling back codex in a project that never installed it must NOT run the
    global removers (they'd rip out another project's registration) (#2)."""
    result = runner.invoke(
        app, ["install", "codex", "--rollback"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert sandbox["native_cmds"] == []  # no removers fired
    assert "Nothing to roll back" in result.stderr


def test_codex_write_without_cli_says_run_yourself(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--write` on a host without the `codex` CLI must NOT claim 'Ran:' — the
    commands were only printed (Greptile P1)."""
    def _print_only(cmds: list, *, run: bool, cwd: str | None = None) -> list:
        return [{"cmd": " ".join(c), "ran": False, "ok": None, "detail": ""}
                for c in cmds]

    monkeypatch.setattr(install_mod, "_run_or_print", _print_only)
    result = runner.invoke(app, ["install", "codex", "--write"], catch_exceptions=False)
    assert "Ran:" not in result.stderr
    assert "codex not on PATH" in result.stderr


def test_codex_automations_installed_paused(sandbox: dict[str, Path]) -> None:
    """`--automations` materializes the templates into ~/.codex/automations/,
    PAUSED, with this project's cwds filled in."""
    import tomllib

    runner.invoke(
        app, ["install", "codex", "--write", "--automations"], catch_exceptions=False
    )
    base = sandbox["home"] / ".codex" / "automations"
    dirs = sorted(p.name for p in base.iterdir()) if base.is_dir() else []
    assert dirs, "expected automation dirs"
    for d in dirs:
        toml = tomllib.loads((base / d / "automation.toml").read_text())
        assert toml["status"] == "PAUSED"  # never auto-active
        assert toml["id"] == d  # id matches dir name
        assert toml["cwds"] == [str(sandbox["project"])]  # project filled in
        assert (base / d / "memory.md").is_file()


def test_codex_automations_rerun_preserves_live_state(
    sandbox: dict[str, Path]
) -> None:
    """Re-running --automations must NOT clobber an automation's accrued memory.md
    or the user's edits to automation.toml (review Finding 1)."""
    runner.invoke(
        app, ["install", "codex", "--write", "--automations"], catch_exceptions=False
    )
    d = next((sandbox["home"] / ".codex" / "automations").iterdir())
    # Codex accrues run history; the user retunes the automation.
    (d / "memory.md").write_text("run history line 1\n")
    (d / "automation.toml").write_text(
        (d / "automation.toml").read_text() + "\n# user-tuned\n"
    )

    runner.invoke(
        app, ["install", "codex", "--write", "--automations"], catch_exceptions=False
    )
    assert (d / "memory.md").read_text() == "run history line 1\n"  # not truncated
    assert "# user-tuned" in (d / "automation.toml").read_text()  # edit preserved


def test_codex_automations_namespaced_by_full_path(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two projects that share a basename must render to DIFFERENT automation dirs
    (review Finding 2 — basename-only namespacing collided)."""
    def ids_for(root: str) -> set[str]:
        monkeypatch.setattr(install_mod, "_project_root", lambda: Path(root))
        return {a["id"] for a in install_mod._codex_automation_plan()}

    a = ids_for("/work/a/app")
    b = ids_for("/work/b/app")  # same basename "app", different path
    assert a and b
    assert a.isdisjoint(b)  # no collision


def test_codex_automations_rejected_for_other_harness(
    sandbox: dict[str, Path]
) -> None:
    """`--automations` is Codex-only — other harnesses refuse cleanly."""
    result = runner.invoke(app, ["install", "cursor", "--write", "--automations"])
    assert result.exit_code == 2
    assert "Codex-only" in result.stderr


def test_codex_automations_dry_run_writes_nothing(sandbox: dict[str, Path]) -> None:
    """Without --write, --automations previews but writes no automation dirs."""
    result = runner.invoke(
        app, ["install", "codex", "--automations"], catch_exceptions=False
    )
    assert "Automations" in result.stderr and "PAUSED" in result.stderr
    assert not (sandbox["home"] / ".codex" / "automations").exists()


def test_codex_automations_removed_on_rollback(sandbox: dict[str, Path]) -> None:
    """Rollback deletes the automation dirs anvil created."""
    runner.invoke(
        app, ["install", "codex", "--write", "--automations"], catch_exceptions=False
    )
    base = sandbox["home"] / ".codex" / "automations"
    assert list(base.iterdir())  # created

    runner.invoke(app, ["install", "codex", "--rollback"], catch_exceptions=False)
    assert not base.exists() or not list(base.iterdir())  # gone


def test_codex_env_flag_in_generated_command(sandbox: dict[str, Path]) -> None:
    """`--root` pins ANVIL_ROOT, which must surface as a `--env` in `mcp add` (#10)."""
    runner.invoke(
        app, ["install", "codex", "--write", "--root", "/work/proj"],
        catch_exceptions=False,
    )
    mcp_add = next(
        " ".join(c) for c in sandbox["native_cmds"] if c[:3] == ["codex", "mcp", "add"]
    )
    assert "--env ANVIL_ROOT=/work/proj" in mcp_add


def test_rollback_strips_block_from_adopted_instruction_file(
    sandbox: dict[str, Path]
) -> None:
    """anvil creates AGENTS.md, the user then adopts it (adds their own prose around
    our block). Rollback must STRIP our block, not delete the file (#1/#8)."""
    instr = sandbox["project"] / "AGENTS.md"
    runner.invoke(app, ["install", "codex", "--write"], catch_exceptions=False)
    # User adopts the created file, adding prose above and below our block.
    body = instr.read_text()
    instr.write_text(f"# My house rules\n\n{body}\nKeep this line too.\n")

    runner.invoke(app, ["install", "codex", "--rollback"], catch_exceptions=False)
    assert instr.is_file()  # NOT deleted
    text = instr.read_text()
    assert "BEGIN ANVIL" not in text  # our block gone
    assert "# My house rules" in text  # user prose above survives
    assert "Keep this line too." in text  # user prose below survives


def test_dangling_symlink_dest_is_refused(sandbox: dict[str, Path]) -> None:
    """A BROKEN symlinked instruction dest is refused — writing through it would
    create an un-rollback-able footprint (#4). (A valid symlink is allowed.)"""
    link = sandbox["project"] / "AGENTS.md"
    link.symlink_to(sandbox["home"] / "nonexistent-target.md")  # dangling

    result = runner.invoke(app, ["install", "codex", "--write"])
    assert result.exit_code == 2
    assert "symlink" in result.stderr.lower()


def test_instruction_file_new_is_marked_block(sandbox: dict[str, Path]) -> None:
    """A fresh instruction file holds the AGENTS.md content inside anvil markers."""
    result = runner.invoke(
        app, ["install", "codex", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    instr = sandbox["project"] / "AGENTS.md"
    assert instr.is_file()
    text = instr.read_text()
    assert "BEGIN ANVIL" in text and "END ANVIL" in text
    # The full AGENTS.md content is present (just wrapped).
    agents = (_repo_root() / "AGENTS.md").read_text().strip()
    assert agents in text


def test_instruction_file_preserves_user_content(sandbox: dict[str, Path]) -> None:
    """A pre-existing user AGENTS.md is preserved; our block is appended, not over
    it. Re-running is idempotent (one block) and rollback removes only our block."""
    instr = sandbox["project"] / "AGENTS.md"
    instr.write_text("# My rules\nDo not delete this.\n")

    runner.invoke(app, ["install", "codex", "--write"], catch_exceptions=False)
    text = instr.read_text()
    assert text.startswith("# My rules\nDo not delete this.")  # user content first
    assert "BEGIN ANVIL" in text

    # Idempotent: a second write does not duplicate the block.
    runner.invoke(app, ["install", "codex", "--write"], catch_exceptions=False)
    assert instr.read_text().count("BEGIN ANVIL") == 1

    # Rollback restores the user's original file byte-for-byte.
    runner.invoke(app, ["install", "codex", "--rollback"], catch_exceptions=False)
    assert instr.read_text() == "# My rules\nDo not delete this.\n"


def test_rollback_restores_config_and_removes_created(
    sandbox: dict[str, Path],
) -> None:
    """Rollback restores a modified JSON config from backup (cursor — an MCP-only
    harness anvil writes a config file for, and never an AGENTS.md)."""
    cfg = sandbox["home"] / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": {"other": {"command": "x"}}})
    cfg.write_text(original)

    runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    assert "anvil" in json.loads(cfg.read_text())["mcpServers"]
    assert not (sandbox["project"] / "AGENTS.md").exists()  # MCP-only: no splice

    result = runner.invoke(
        app, ["install", "cursor", "--rollback"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Config restored to the user's original (no anvil server).
    assert cfg.read_text() == original


def test_instruction_refuses_ambiguous_markers(sandbox: dict[str, Path]) -> None:
    """A stray END marker (no BEGIN) in user prose must NOT be treated as our block
    — install refuses rather than risk corrupting/duplicating the file (#7/#13)."""
    instr = sandbox["project"] / "AGENTS.md"
    instr.write_text("# notes\nSee <!-- END ANVIL --> for details.\n")
    before = instr.read_text()
    result = runner.invoke(app, ["install", "codex", "--write"])
    assert result.exit_code == 2  # clean refusal, not a traceback
    assert "Error:" in result.stderr and "marker" in result.stderr
    assert instr.read_text() == before  # untouched — no corruption


def test_instruction_idempotent_and_faithful_strip(sandbox: dict[str, Path]) -> None:
    """Re-running yields a byte-identical file (one block), and rollback restores
    the user's ORIGINAL bytes exactly (#18 faithful strip)."""
    instr = sandbox["project"] / "AGENTS.md"
    original = "# my rules\n\nline two\n"
    instr.write_text(original)
    runner.invoke(app, ["install", "codex", "--write"], catch_exceptions=False)
    once = instr.read_text()
    runner.invoke(app, ["install", "codex", "--write"], catch_exceptions=False)
    assert instr.read_text() == once  # idempotent
    runner.invoke(app, ["install", "codex", "--rollback"], catch_exceptions=False)
    assert instr.read_text() == original  # byte-faithful restore


def test_crash_before_writes_completed_is_still_reversible(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a write crashes mid-install, the manifest was already persisted, so
    rollback still restores the user's config (#2 crash safety)."""
    cfg = sandbox["home"] / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": {"other": {"command": "x"}}})
    cfg.write_text(original)

    # Let the MCP config write LAND, then die the instant it's on disk — the
    # manifest is recorded BEFORE any write, so the mutation must still be
    # rollback-able. The patch is class-level (pathlib has no per-instance hook) but
    # only the EXACT config path raises, so no other write in-process is affected;
    # the scoped context undoes it (and never touches HOME isolation).
    real_write_text = install_mod.Path.write_text

    def _write_then_crash(self: Path, *a: object, **k: object) -> int:
        out = real_write_text(self, *a, **k)
        if str(self) == str(cfg):
            raise OSError("boom")
        return out

    with monkeypatch.context() as mctx:
        mctx.setattr(install_mod.Path, "write_text", _write_then_crash)
        runner.invoke(app, ["install", "cursor", "--write"])  # crashes mid-write
    assert "anvil" in json.loads(cfg.read_text())["mcpServers"]  # config mutated

    result = runner.invoke(app, ["install", "cursor", "--rollback"])
    assert result.exit_code == 0
    assert cfg.read_text() == original  # recoverable despite the crash


def test_opencode_writes_config(sandbox: dict[str, Path]) -> None:
    """opencode install merges the anvil server into opencode.json (MCP-only — no
    AGENTS.md splice).

    OpenCode's entry shape is unique: argv-array `command`, `type: "local"`,
    `enabled: true`.
    """
    result = runner.invoke(
        app, ["install", "opencode", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    cfg = sandbox["project"] / "opencode.json"
    assert cfg.is_file(), f"expected {cfg} to exist"
    written = json.loads(cfg.read_text(encoding="utf-8"))
    # A fresh install seeds the full block, so the $schema hint is preserved
    # (matches `mcp-config opencode` output + the committed reference).
    assert written["$schema"] == "https://opencode.ai/config.json"
    spec = written["mcp"]["anvil"]
    assert spec["type"] == "local"
    assert isinstance(spec["command"], list)
    _assert_checkout_argv(spec["command"])
    assert spec["enabled"] is True
    # MCP-only: no AGENTS.md splice.
    assert not (sandbox["project"] / "AGENTS.md").exists()


def test_opencode_merge_preserves_existing_keys(sandbox: dict[str, Path]) -> None:
    """Merging into an existing opencode.json keeps unrelated keys + servers."""
    cfg = sandbox["project"] / "opencode.json"
    cfg.write_text(
        json.dumps({"$schema": "x", "theme": "dark", "mcp": {"other": {"type": "local"}}}),
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["install", "opencode", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"  # unrelated top-level key preserved
    assert "other" in data["mcp"]  # pre-existing server preserved
    assert data["mcp"]["anvil"]["type"] == "local"  # ours added


def test_roo_writes_project_mcp_json(sandbox: dict[str, Path]) -> None:
    """roo install writes .roo/mcp.json (mcpServers); MCP-only — no AGENTS.md."""
    result = runner.invoke(app, ["install", "roo", "--write"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout + result.stderr
    cfg = sandbox["project"] / ".roo" / "mcp.json"
    assert cfg.is_file(), f"expected {cfg} to exist"
    spec = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["anvil"]
    _assert_checkout_launcher(spec)
    assert not (sandbox["project"] / "AGENTS.md").exists()


def test_amp_writes_flat_dotted_key(sandbox: dict[str, Path]) -> None:
    """amp install merges the flat `amp.mcpServers` key into ~/.config/amp/settings.json."""
    result = runner.invoke(app, ["install", "amp", "--write"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout + result.stderr
    cfg = sandbox["home"] / ".config" / "amp" / "settings.json"
    assert cfg.is_file(), f"expected {cfg} to exist"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    # The dotted key is a single flat settings key, not a nested table.
    assert "amp.mcpServers" in data
    _assert_checkout_launcher(data["amp.mcpServers"]["anvil"])


def test_yaml_harnesses_skip_mcp_write(sandbox: dict[str, Path]) -> None:
    """continue/goose have no in-place YAML merge writer: MCP is skipped (and being
    MCP-only, nothing else is written either)."""
    for harness in ("continue", "goose"):
        result = runner.invoke(
            app, ["install", "--json", harness], catch_exceptions=False
        )
        data = json.loads(result.stdout.strip())["data"]
        assert data["mcp"]["action"] == "skipped"
        assert data["mcp"]["note"]  # note points at `anvil mcp-config <harness>`


def test_root_flag_propagates_into_written_block(sandbox: dict[str, Path]) -> None:
    """`--root /x` puts env.ANVIL_ROOT into the written MCP server block."""
    result = runner.invoke(
        app, ["install", "cursor", "--write", "--root", "/x"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    cfg = sandbox["home"] / ".cursor" / "mcp.json"
    spec = json.loads(cfg.read_text())["mcpServers"]["anvil"]
    assert spec["env"]["ANVIL_ROOT"] == "/x"


def test_uv_run_flag_propagates_into_written_block(sandbox: dict[str, Path]) -> None:
    """`--uv-run` emits the explicit uv invocation in the written block."""
    result = runner.invoke(
        app, ["install", "cursor", "--write", "--uv-run"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    cfg = sandbox["home"] / ".cursor" / "mcp.json"
    spec = json.loads(cfg.read_text())["mcpServers"]["anvil"]
    assert spec["command"] == "uv"
    assert spec["args"][0] == "run"
    assert "anvil.mcp_server" in spec["args"]


def test_unknown_harness_fails(sandbox: dict[str, Path]) -> None:
    """Bad harness exits 2; under --json emits error.code == bad_request."""
    result = runner.invoke(app, ["install", "nope"], catch_exceptions=False)
    assert result.exit_code == 2

    j = runner.invoke(app, ["install", "--json", "nope"], catch_exceptions=False)
    assert j.exit_code == 2
    env = json.loads(j.stdout.strip())
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_request"


# --- pi: native package delivery --------------------------------------------------


def test_pi_native_commands_generated(sandbox: dict[str, Path]) -> None:
    """`install pi --write` drives the pi CLI: `pi install -l <abs package dir>`."""
    result = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout + result.stderr
    cmds = [" ".join(c) for c in sandbox["native_cmds"]]
    assert cmds, "local checkout packaging/pi/anvil-pi must resolve from a repo checkout"
    install_cmd = next(c for c in sandbox["native_cmds"] if c[:2] == ["pi", "install"])
    assert install_cmd[2] == "-l", install_cmd  # project scope (matches anvil's manifest)
    spec = install_cmd[3]
    assert spec.startswith("/"), f"spec must be an absolute path: {spec}"
    assert spec.endswith("packaging/pi/anvil-pi"), spec
    # No MCP config, no instruction splice — pi reads AGENTS.md natively and has
    # no MCP client.
    assert not (sandbox["project"] / "AGENTS.md").exists()
    assert not (sandbox["home"] / ".codex").exists()


def test_pi_env_override_uses_verbatim_spec(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANVIL_PI_PACKAGE is passed through verbatim (teams / wheel installs)."""
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "git:github.com/fakoli/anvil-pi@v1.2.3")
    result = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout + result.stderr
    install_cmd = next(c for c in sandbox["native_cmds"] if c[:2] == ["pi", "install"])
    assert install_cmd[3] == "git:github.com/fakoli/anvil-pi@v1.2.3"


def test_pi_no_checkout_prints_guidance(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a checkout and without ANVIL_PI_PACKAGE: guidance, no fabricated command."""
    monkeypatch.setattr(install_mod, "_pi_local_package_dir", lambda: Path("/nonexistent/anvil-pi"))
    result = runner.invoke(app, ["install", "pi"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout + result.stderr
    assert sandbox["native_cmds"] == [], "must not fabricate a remote spec"
    assert "ANVIL_PI_PACKAGE" in result.stderr
    # JSON envelope carries the same note
    rj = runner.invoke(app, ["install", "pi", "--json"], catch_exceptions=False)
    payload = json.loads(rj.stdout)
    assert "ANVIL_PI_PACKAGE" in payload["data"]["note"]


def test_pi_rollback_uses_remove_and_ignores_refcount(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """pi installs are project-scoped: rollback removes even if other projects
    still have installs recorded (unlike the global codex/openclaw routes)."""
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "git:github.com/fakoli/anvil-pi@v1.2.3")
    write = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert write.exit_code == 0, write.stdout + write.stderr
    # Simulate another project having an install recorded (global refcount > 0).
    manifest = install_mod._load_manifest()
    manifest["installs"]["/some/other/project::pi"] = {"paths": {}}
    install_mod._save_manifest(manifest)
    rb = runner.invoke(app, ["install", "pi", "--rollback", "--json"], catch_exceptions=False)
    assert rb.exit_code == 0, rb.stdout + rb.stderr
    payload = json.loads(rb.stdout)
    cmds = [entry["cmd"] for entry in payload["data"]["native"]]
    assert any(c.startswith("pi remove -l git:github.com/fakoli/anvil-pi@v1.2.3") for c in cmds), cmds


# --- pi rollback: record-driven target (astra round-2 major) -----------------------


def test_pi_rollback_targets_recorded_package_when_env_changes(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install A via override, change the override to B: rollback removes A."""
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "git:github.com/fakoli/anvil-pi@v1.0.0")
    write = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert write.exit_code == 0, write.stdout + write.stderr
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "git:github.com/fakoli/anvil-pi@v2.0.0")
    rb = runner.invoke(app, ["install", "pi", "--rollback", "--json"], catch_exceptions=False)
    assert rb.exit_code == 0, rb.stdout + rb.stderr
    cmds = [entry["cmd"] for entry in json.loads(rb.stdout)["data"]["native"]]
    assert any("pi remove -l git:github.com/fakoli/anvil-pi@v1.0.0" in c for c in cmds), cmds
    assert not any("v2.0.0" in c for c in cmds)
    # record dropped after VERIFIED removal (exact project-qualified key)
    assert install_mod._load_manifest()["installs"].get(install_mod._install_key("pi")) is None


def test_pi_rollback_fail_closed_without_recorded_identity(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy record with no native_package: re-deriving the spec from the current
    environment is UNSAFE (env/checkouts drift) — fail closed, keep the record."""
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "npm:@fakoli/anvil-pi@0.1.0")
    manifest = install_mod._load_manifest()
    manifest["installs"][install_mod._install_key("pi")] = {
        "ts": "2026-01-01T00:00:00+00:00", "paths": []
    }  # legacy shape: no native_package
    install_mod._save_manifest(manifest)
    rb = runner.invoke(app, ["install", "pi", "--rollback", "--json"], catch_exceptions=False)
    assert rb.exit_code == 0, rb.stdout + rb.stderr
    payload = json.loads(rb.stdout)
    assert payload["data"]["native"] == [], "must not re-derive a removal target"
    assert "no recorded pi package identity" in payload["data"]["note"]
    record = install_mod._load_manifest()["installs"].get(install_mod._install_key("pi"))
    assert record is not None, "record preserved for manual resolution"


def test_pi_rollback_ignores_relocated_checkout(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install from checkout A, relocate the checkout: rollback removes ONLY the
    recorded A spec (the relocated path must never be derived or removed)."""
    write = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert write.exit_code == 0, write.stdout + write.stderr
    original_spec = next(
        c for c in sandbox["native_cmds"] if c[:2] == ["pi", "install"]
    )[3]
    monkeypatch.setattr(install_mod, "_pi_local_package_dir", lambda: Path("/relocated/anvil-pi"))
    rb = runner.invoke(app, ["install", "pi", "--rollback", "--json"], catch_exceptions=False)
    assert rb.exit_code == 0, rb.stdout + rb.stderr
    cmds = [entry["cmd"] for entry in json.loads(rb.stdout)["data"]["native"]]
    assert any(f"pi remove -l {original_spec}" in c for c in cmds), cmds
    assert not any("/relocated/anvil-pi" in c for c in cmds)
    assert install_mod._load_manifest()["installs"].get(install_mod._install_key("pi")) is None


def test_pi_rollback_preserves_record_on_failed_removal(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removal that doesn't complete keeps the install record for a retry."""
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "npm:@fakoli/anvil-pi@0.1.0")
    write = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert write.exit_code == 0, write.stdout + write.stderr
    key = install_mod._install_key("pi")
    assert "native_package" in install_mod._load_manifest()["installs"][key]

    def _failing_run(cmds: list, *, run: bool, cwd: str | None = None) -> list:
        return [
            {"cmd": " ".join(c), "ran": run, "ok": False, "detail": "pi: project is not trusted"}
            for c in cmds
        ]

    successful_fake = install_mod._run_or_print
    monkeypatch.setattr(install_mod, "_run_or_print", _failing_run)
    rb = runner.invoke(app, ["install", "pi", "--rollback", "--json"], catch_exceptions=False)
    assert rb.exit_code == 0, rb.stdout + rb.stderr
    payload = json.loads(rb.stdout)
    assert "install record" in payload["data"]["note"]
    # persisted settings still list the package — that's WHY the record is kept
    settings = install_mod._pi_settings_path(install_mod._project_root())
    assert json.loads(settings.read_text())["packages"] == ["npm:@fakoli/anvil-pi@0.1.0"]
    # record still there, rollback retryable
    record = install_mod._load_manifest()["installs"].get(key)
    assert record is not None and record["native_package"] == "npm:@fakoli/anvil-pi@0.1.0"
    # successful retry drops it
    monkeypatch.setattr(install_mod, "_run_or_print", successful_fake)
    rb2 = runner.invoke(app, ["install", "pi", "--rollback", "--json"], catch_exceptions=False)
    assert rb2.exit_code == 0, rb2.stdout + rb2.stderr
    assert install_mod._load_manifest()["installs"].get(key) is None
    # and the persisted settings no longer list the package either
    assert json.loads(settings.read_text())["packages"] == []


def test_pi_missing_binary_preserves_record(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """run=False (binary absent) must never drop the record — nothing was tried."""
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "npm:@fakoli/anvil-pi@0.1.0")
    write = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert write.exit_code == 0, write.stdout + write.stderr
    key = install_mod._install_key("pi")

    def _no_binary_run(cmds: list, *, run: bool, cwd: str | None = None) -> list:
        return [{"cmd": " ".join(c), "ran": False, "ok": None, "detail": ""} for c in cmds]

    monkeypatch.setattr(install_mod, "_run_or_print", _no_binary_run)
    rb = runner.invoke(app, ["install", "pi", "--rollback", "--json"], catch_exceptions=False)
    assert rb.exit_code == 0, rb.stdout + rb.stderr
    payload = json.loads(rb.stdout)
    assert "pi CLI not on PATH" in payload["data"]["note"]
    assert install_mod._load_manifest()["installs"].get(key) is not None


def test_pi_unverifiable_persistence_preserves_record(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """pi reporting success is not proof: if persisted settings can't be read,
    keep the record (pi can exit 0 on an in-memory change with a failed write)."""
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "npm:@fakoli/anvil-pi@0.1.0")
    write = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert write.exit_code == 0, write.stdout + write.stderr
    settings = install_mod._pi_settings_path(install_mod._project_root())
    settings.write_text("{not json")  # simulate a failed/corrupt persistence
    rb = runner.invoke(app, ["install", "pi", "--rollback", "--json"], catch_exceptions=False)
    assert rb.exit_code == 0, rb.stdout + rb.stderr
    payload = json.loads(rb.stdout)
    assert "could not verify" in payload["data"]["note"]
    assert install_mod._load_manifest()["installs"].get(install_mod._install_key("pi")) is not None


def test_pi_absent_removal_despite_exit_1_completes(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed remove command whose persisted settings no longer list the
    package (e.g. pi's 'No matching package found' exit 1) is a COMPLETED
    rollback — the record drops, with an explanatory note."""
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "npm:@fakoli/anvil-pi@0.1.0")
    write = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert write.exit_code == 0, write.stdout + write.stderr
    # package vanished from settings out-of-band (e.g. removed by hand)
    settings = install_mod._pi_settings_path(install_mod._project_root())
    settings.write_text(json.dumps({"packages": []}))

    def _exit1_run(cmds: list, *, run: bool, cwd: str | None = None) -> list:
        return [
            {"cmd": " ".join(c), "ran": run, "ok": False,
             "detail": "No matching package found"}
            for c in cmds
        ]

    monkeypatch.setattr(install_mod, "_run_or_print", _exit1_run)
    rb = runner.invoke(app, ["install", "pi", "--rollback", "--json"], catch_exceptions=False)
    assert rb.exit_code == 0, rb.stdout + rb.stderr
    payload = json.loads(rb.stdout)
    assert "treating as removed" in payload["data"]["note"]
    assert install_mod._load_manifest()["installs"].get(install_mod._install_key("pi")) is None


def test_pi_guidance_write_does_not_record(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No checkout and no override → guidance-only --write must NOT create (or
    overwrite) an install record: there is nothing to roll back."""
    monkeypatch.setattr(install_mod, "_pi_local_package_dir", lambda: Path("/nonexistent/anvil-pi"))
    # a pre-existing record with a real identity must survive untouched
    manifest = install_mod._load_manifest()
    manifest["installs"][install_mod._install_key("pi")] = {
        "ts": "2026-01-01T00:00:00+00:00", "paths": [],
        "native_package": "npm:@fakoli/anvil-pi@0.0.9",
    }
    install_mod._save_manifest(manifest)
    result = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout + result.stderr
    record = install_mod._load_manifest()["installs"].get(install_mod._install_key("pi"))
    assert record is not None and record["native_package"] == "npm:@fakoli/anvil-pi@0.0.9"


def test_pi_refuses_overwrite_of_recorded_target(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing a DIFFERENT spec while one is recorded would overwrite the only
    rollback identity before the new install succeeds — refuse with guidance."""
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "npm:@fakoli/anvil-pi@0.1.0")
    first = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert first.exit_code == 0, first.stdout + first.stderr
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "npm:@fakoli/anvil-pi@0.2.0")
    second = runner.invoke(app, ["install", "pi", "--write", "--json"], catch_exceptions=False)
    assert second.exit_code == 2, second.stdout + second.stderr
    payload = json.loads(second.stdout)
    assert "rollback" in payload["error"]["message"]
    # the original record is intact
    record = install_mod._load_manifest()["installs"].get(install_mod._install_key("pi"))
    assert record["native_package"] == "npm:@fakoli/anvil-pi@0.1.0"
    # re-installing the SAME spec is allowed (idempotent refresh)
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "npm:@fakoli/anvil-pi@0.1.0")
    same = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert same.exit_code == 0, same.stdout + same.stderr


def test_pi_two_actual_projects_pin_their_own_cwd(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """pi resolves -l scope from process.cwd(), NOT from anvil's manifest key —
    the recorded native_cwd must pin every pi subprocess to the project the
    manifest records. Two real ANVIL_ROOT/checkout combinations."""
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "npm:@fakoli/anvil-pi@0.1.0")
    w1 = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert w1.exit_code == 0, w1.stdout + w1.stderr
    assert sandbox["native_cwds"][-1] == str(sandbox["project"]), "install pinned to the manifest project"
    # second project, different spec: distinct key, own cwd
    project2 = sandbox["project"].parent / "project2"
    project2.mkdir()
    monkeypatch.setenv("ANVIL_ROOT", str(project2))
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "npm:@fakoli/anvil-pi@0.2.0")
    w2 = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert w2.exit_code == 0, w2.stdout + w2.stderr
    assert sandbox["native_cwds"][-1] == str(project2), "second install pinned to project2"
    assert (project2 / ".pi" / "settings.json").is_file()
    assert json.loads((project2 / ".pi" / "settings.json").read_text())["packages"] == ["npm:@fakoli/anvil-pi@0.2.0"]
    # rollback project2: cwd pinned to project2, only its spec removed
    rb = runner.invoke(app, ["install", "pi", "--rollback", "--json"], catch_exceptions=False)
    assert rb.exit_code == 0, rb.stdout + rb.stderr
    assert sandbox["native_cwds"][-1] == str(project2), "rollback pinned to the recorded native_cwd"
    cmds = [entry["cmd"] for entry in json.loads(rb.stdout)["data"]["native"]]
    assert any("remove -l npm:@fakoli/anvil-pi@0.2.0" in c for c in cmds)
    assert json.loads((project2 / ".pi" / "settings.json").read_text())["packages"] == []
    # project1 untouched
    assert json.loads((sandbox["project"] / ".pi" / "settings.json").read_text())["packages"] == ["npm:@fakoli/anvil-pi@0.1.0"]


def test_pi_two_projects_remove_their_own_specs(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two project installs with different specs: each rollback removes only
    its own recorded package."""
    monkeypatch.setenv("ANVIL_PI_PACKAGE", "npm:@fakoli/anvil-pi@0.1.0")
    write1 = runner.invoke(app, ["install", "pi", "--write"], catch_exceptions=False)
    assert write1.exit_code == 0, write1.stdout + write1.stderr
    # simulate a second project's install with a different spec
    manifest = install_mod._load_manifest()
    other_key = "/some/other/project::pi"
    manifest["installs"][other_key] = {
        "ts": "2026-01-01T00:00:00+00:00",
        "paths": [],
        "native_package": "npm:@fakoli/anvil-pi@0.2.0",
    }
    install_mod._save_manifest(manifest)
    rb = runner.invoke(app, ["install", "pi", "--rollback", "--json"], catch_exceptions=False)
    assert rb.exit_code == 0, rb.stdout + rb.stderr
    cmds = [entry["cmd"] for entry in json.loads(rb.stdout)["data"]["native"]]
    assert any("remove -l npm:@fakoli/anvil-pi@0.1.0" in c for c in cmds), cmds
    assert not any("0.2.0" in c for c in cmds)
    # the other project's record survives untouched
    assert install_mod._load_manifest()["installs"].get(other_key) is not None
