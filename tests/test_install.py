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

    def _fake_run(cmds: list, *, run: bool) -> list:
        ran.extend(cmds)
        return [
            {"cmd": " ".join(c), "ran": run, "ok": True if run else None, "detail": ""}
            for c in cmds
        ]

    monkeypatch.setattr(install_mod, "_run_or_print", _fake_run)
    return {"home": home, "project": project, "native_cmds": ran}


def test_known_harnesses_present() -> None:
    """The verified harnesses from the spec are all in the registry."""
    for name in ("codex", "copilot", "gemini", "openclaw", "cursor", "windsurf",
                 "cline", "zed", "openhands", "opencode", "roo", "amp",
                 "continue", "goose"):
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

    # The dry-run surfaces SOMETHING per harness (on stderr — stdout stays clean):
    # the instruction path for file-writing harnesses, or the native commands for
    # the ones that install via their own CLI (codex, openclaw).
    if HARNESSES[harness].writes_instructions:
        assert "Instruction file" in result.stderr
    else:
        assert "Run these" in result.stderr


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
    mcp_add = next(c for c in cmds if c.startswith("codex mcp add anvil"))
    assert "-- bash" in mcp_add and "anvil-mcp" in mcp_add


def test_codex_skips_agents_skills_drop(sandbox: dict[str, Path]) -> None:
    """Codex gets skills via its plugin, so the neutral .agents/skills/ drop is
    skipped — but AGENTS.md still gets the marked block (Codex reads it)."""
    runner.invoke(app, ["install", "codex", "--write"], catch_exceptions=False)
    assert not (sandbox["project"] / ".agents" / "skills").exists()
    assert "BEGIN ANVIL" in (sandbox["project"] / "AGENTS.md").read_text()


def test_openclaw_native_commands_generated(sandbox: dict[str, Path]) -> None:
    """OpenClaw installs via its own CLI: `mcp add` (--no-probe) + `plugins
    install` from anvil's Claude-compatible marketplace."""
    result = runner.invoke(
        app, ["install", "openclaw", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    cmds = [" ".join(c) for c in sandbox["native_cmds"]]
    mcp_add = next(c for c in cmds if c.startswith("openclaw mcp add anvil"))
    # --no-probe: a cold-venv probe timeout must not block the save (half-install).
    assert "--no-probe" in mcp_add
    assert "--command bash" in mcp_add and "anvil-mcp" in mcp_add and "--arg" in mcp_add
    # --force: re-install refreshes the plugin instead of a silent "already exists".
    assert "openclaw plugins install anvil --marketplace fakoli/anvil --force" in cmds


def test_native_command_failure_is_surfaced(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native command that RAN and failed must show a `⚠` with detail, not pass
    silently (review Finding 3 — exit-0-with-error misclassification)."""
    def _failing(cmds: list, *, run: bool) -> list:
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
    def _print_only(cmds: list, *, run: bool) -> list:
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
    runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    # User adopts the created file, adding prose above and below our block.
    body = instr.read_text()
    instr.write_text(f"# My house rules\n\n{body}\nKeep this line too.\n")

    runner.invoke(app, ["install", "cursor", "--rollback"], catch_exceptions=False)
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

    result = runner.invoke(app, ["install", "cursor", "--write"])
    assert result.exit_code == 2
    assert "symlink" in result.stderr.lower()


def test_instruction_file_new_is_marked_block(sandbox: dict[str, Path]) -> None:
    """A fresh instruction file holds the AGENTS.md content inside anvil markers."""
    result = runner.invoke(
        app, ["install", "cursor", "--write"], catch_exceptions=False
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

    runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    text = instr.read_text()
    assert text.startswith("# My rules\nDo not delete this.")  # user content first
    assert "BEGIN ANVIL" in text

    # Idempotent: a second write does not duplicate the block.
    runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    assert instr.read_text().count("BEGIN ANVIL") == 1

    # Rollback restores the user's original file byte-for-byte.
    runner.invoke(app, ["install", "cursor", "--rollback"], catch_exceptions=False)
    assert instr.read_text() == "# My rules\nDo not delete this.\n"


def test_copilot_instruction_dest_is_marked_block(sandbox: dict[str, Path]) -> None:
    """Copilot's instruction dest is .github/copilot-instructions.md with our block."""
    result = runner.invoke(
        app, ["install", "copilot", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    instr = sandbox["project"] / ".github" / "copilot-instructions.md"
    assert instr.is_file()
    text = instr.read_text()
    assert "BEGIN ANVIL" in text
    assert (_repo_root() / "AGENTS.md").read_text().strip() in text


def test_rollback_restores_config_and_removes_created(
    sandbox: dict[str, Path],
) -> None:
    """Rollback restores a modified JSON config from backup and deletes files we
    made (cursor — a harness anvil DOES write a config file for)."""
    cfg = sandbox["home"] / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": {"other": {"command": "x"}}})
    cfg.write_text(original)

    runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    assert "anvil" in json.loads(cfg.read_text())["mcpServers"]
    assert (sandbox["project"] / "AGENTS.md").is_file()  # created by us

    result = runner.invoke(
        app, ["install", "cursor", "--rollback"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Config restored to the user's original (no anvil server).
    assert cfg.read_text() == original
    # The instruction file we created is gone.
    assert not (sandbox["project"] / "AGENTS.md").exists()


def test_skills_dropped_into_agents_skills(sandbox: dict[str, Path]) -> None:
    """Install drops anvil's SKILL.md set into the neutral .agents/skills/ path,
    namespaced anvil-*, and rollback removes the dirs it created."""
    runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    skills_base = sandbox["project"] / ".agents" / "skills"
    sources = sorted(
        d.name
        for d in (_repo_root() / "skills").iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    )
    assert sources, "expected anvil to ship skills"
    for name in sources:
        skill = skills_base / f"anvil-{name}" / "SKILL.md"
        assert skill.is_file(), f"expected {skill}"

    # Idempotent re-run: still one dir per skill, no nesting/dupes.
    runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    assert sorted(p.name for p in skills_base.iterdir()) == [
        f"anvil-{n}" for n in sources
    ]

    # Rollback removes the skill dirs we created (and leaves no .anvil-bak litter).
    runner.invoke(app, ["install", "cursor", "--rollback"], catch_exceptions=False)
    for name in sources:
        assert not (skills_base / f"anvil-{name}").exists()
        assert not (skills_base / f"anvil-{name}{'.anvil-bak'}").exists()


def test_skills_preserve_user_owned_skill_dir(sandbox: dict[str, Path]) -> None:
    """A user's pre-existing .agents/skills/anvil-plan is backed up and restored on
    rollback (we never silently eat a dir that happens to share our namespace)."""
    skills_base = sandbox["project"] / ".agents" / "skills"
    user_skill = skills_base / "anvil-plan"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("# my own plan skill\n")

    runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    # Ours replaced it for the duration...
    assert "my own plan skill" not in (user_skill / "SKILL.md").read_text()

    runner.invoke(app, ["install", "cursor", "--rollback"], catch_exceptions=False)
    # ...but rollback hands the user's original dir back, byte-for-byte.
    assert (user_skill / "SKILL.md").read_text() == "# my own plan skill\n"


def test_instruction_refuses_ambiguous_markers(sandbox: dict[str, Path]) -> None:
    """A stray END marker (no BEGIN) in user prose must NOT be treated as our block
    — install refuses rather than risk corrupting/duplicating the file (#7/#13)."""
    instr = sandbox["project"] / "AGENTS.md"
    instr.write_text("# notes\nSee <!-- END ANVIL --> for details.\n")
    before = instr.read_text()
    result = runner.invoke(app, ["install", "cursor", "--write"])
    assert result.exit_code == 2  # clean refusal, not a traceback
    assert "Error:" in result.stderr and "marker" in result.stderr
    assert instr.read_text() == before  # untouched — no corruption


def test_instruction_idempotent_and_faithful_strip(sandbox: dict[str, Path]) -> None:
    """Re-running yields a byte-identical file (one block), and rollback restores
    the user's ORIGINAL bytes exactly (#18 faithful strip)."""
    instr = sandbox["project"] / "AGENTS.md"
    original = "# my rules\n\nline two\n"
    instr.write_text(original)
    runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    once = instr.read_text()
    runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    assert instr.read_text() == once  # idempotent
    runner.invoke(app, ["install", "cursor", "--rollback"], catch_exceptions=False)
    assert instr.read_text() == original  # byte-faithful restore


def test_shared_agents_md_across_two_harnesses(sandbox: dict[str, Path]) -> None:
    """Two harnesses share the project AGENTS.md. Rolling back ONE keeps the block
    (the other still wants it); rolling back BOTH restores the user's file (#8)."""
    instr = sandbox["project"] / "AGENTS.md"
    instr.write_text("# mine\n")
    runner.invoke(app, ["install", "cursor", "--write"], catch_exceptions=False)
    runner.invoke(app, ["install", "windsurf", "--write"], catch_exceptions=False)

    runner.invoke(app, ["install", "cursor", "--rollback"], catch_exceptions=False)
    assert "BEGIN ANVIL" in instr.read_text()  # windsurf still references it

    runner.invoke(app, ["install", "windsurf", "--rollback"], catch_exceptions=False)
    assert instr.read_text() == "# mine\n"  # last release restores the user's file


def test_crash_before_writes_completed_is_still_reversible(
    sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a write crashes mid-install, the manifest was already persisted, so
    rollback still restores the user's config (#2 crash safety)."""
    cfg = sandbox["home"] / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": {"other": {"command": "x"}}})
    cfg.write_text(original)

    # Make the skills copy explode AFTER the config + manifest are written. A
    # scoped context undoes ONLY this patch (not the sandbox's HOME isolation).
    with monkeypatch.context() as mctx:
        mctx.setattr(
            install_mod.shutil, "copytree",
            lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
        )
        runner.invoke(app, ["install", "cursor", "--write"])  # crashes mid-write
    assert "anvil" in json.loads(cfg.read_text())["mcpServers"]  # config mutated

    result = runner.invoke(app, ["install", "cursor", "--rollback"])
    assert result.exit_code == 0
    assert cfg.read_text() == original  # recoverable despite the crash


@pytest.mark.parametrize("harness", ["gemini", "cline"])
def test_mcp_none_writes_only_instruction(
    harness: str, sandbox: dict[str, Path]
) -> None:
    """`mcp_merge="none"` harness writes ONLY the instruction file + shows note."""
    result = runner.invoke(
        app, ["install", harness, "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Instruction written.
    assert (sandbox["project"] / "AGENTS.md").is_file()
    # No MCP config files anywhere.
    assert not (sandbox["home"] / ".codex").exists()
    assert not (sandbox["home"] / ".cursor").exists()
    # The note is surfaced.
    assert "skipped" in result.stderr

    # And the --json envelope marks the MCP action as skipped.
    j = runner.invoke(
        app, ["install", "--json", harness], catch_exceptions=False
    )
    data = json.loads(j.stdout.strip())["data"]
    assert data["mcp"]["action"] == "skipped"
    assert data["mcp"]["path"] is None
    assert data["mcp"]["note"]  # non-empty note


def test_openhands_writes_agents_md_root(sandbox: dict[str, Path]) -> None:
    """openhands install drops AGENTS.md bytes at the project-root AGENTS.md.

    Current OpenHands reads root AGENTS.md for repo guidelines; the old
    .openhands/microagents/ path is deprecated V0.
    """
    result = runner.invoke(
        app, ["install", "openhands", "--write"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Instruction written at the project-root AGENTS.md (current convention).
    instr = sandbox["project"] / "AGENTS.md"
    assert instr.is_file(), f"expected {instr} to exist"
    assert (_repo_root() / "AGENTS.md").read_text().strip() in instr.read_text()
    # The deprecated microagent path is no longer written.
    assert not (sandbox["project"] / ".openhands" / "microagents" / "anvil.md").exists()
    # No MCP config file (mcp_merge="none").
    assert not (sandbox["home"] / ".codex").exists()
    assert not (sandbox["home"] / ".cursor").exists()
    # Note is surfaced on stderr.
    assert "skipped" in result.stderr

    # JSON envelope: MCP action is skipped, non-empty note.
    j = runner.invoke(
        app, ["install", "--json", "openhands"], catch_exceptions=False
    )
    data = json.loads(j.stdout.strip())["data"]
    assert data["mcp"]["action"] == "skipped"
    assert data["mcp"]["path"] is None
    assert data["mcp"]["note"]


def test_opencode_writes_config_and_agents(sandbox: dict[str, Path]) -> None:
    """opencode install merges the anvil server into opencode.json + drops AGENTS.md.

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
    assert spec["command"][-1].endswith("bin/anvil-mcp")
    assert spec["enabled"] is True
    # AGENTS.md is dropped at the project root (read natively by OpenCode).
    instr = sandbox["project"] / "AGENTS.md"
    assert (_repo_root() / "AGENTS.md").read_text().strip() in instr.read_text()


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
    """roo install writes .roo/mcp.json (mcpServers) + drops AGENTS.md."""
    result = runner.invoke(app, ["install", "roo", "--write"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout + result.stderr
    cfg = sandbox["project"] / ".roo" / "mcp.json"
    assert cfg.is_file(), f"expected {cfg} to exist"
    spec = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["anvil"]
    assert spec["args"][-1].endswith("bin/anvil-mcp")
    assert (sandbox["project"] / "AGENTS.md").is_file()


def test_amp_writes_flat_dotted_key(sandbox: dict[str, Path]) -> None:
    """amp install merges the flat `amp.mcpServers` key into ~/.config/amp/settings.json."""
    result = runner.invoke(app, ["install", "amp", "--write"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout + result.stderr
    cfg = sandbox["home"] / ".config" / "amp" / "settings.json"
    assert cfg.is_file(), f"expected {cfg} to exist"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    # The dotted key is a single flat settings key, not a nested table.
    assert "amp.mcpServers" in data
    assert data["amp.mcpServers"]["anvil"]["args"][-1].endswith("bin/anvil-mcp")


def test_yaml_harnesses_skip_mcp_write(sandbox: dict[str, Path]) -> None:
    """continue/goose have no in-place YAML merge writer: MCP is skipped, AGENTS.md dropped."""
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
