"""Static packaging-manifest validity for the VERIFIED harnesses.

Each manifest under ``packaging/<harness>/`` is checked to parse and carry the
required fields. For NON-verified harnesses we commit a STUB + TODO instead of a
guessed manifest — those STUBs are guarded here too (must contain ``TODO`` and
must NOT contain a JSON/TOML manifest body, so a stub can't silently become a
guessed config).

Layout note: this file lives at ``<repo-root>/tests/`` so ``parents[1]`` is the
repo root (matching ``test_agents_md.py`` / ``test_version_sync.py``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from collections import Counter
from pathlib import Path

import pytest
import yaml


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _packaging() -> Path:
    return _repo_root() / "packaging"


def _pyproject() -> dict:
    return tomllib.loads((_repo_root() / "bin" / "pyproject.toml").read_text(encoding="utf-8"))


def _assert_uv_mcp_spec(spec: dict, project_var: str) -> None:
    assert spec["command"] == "uv"
    assert spec["args"] == [
        "run",
        "--quiet",
        "--project",
        f"${{{project_var}}}/bin",
        "python",
        "-m",
        "anvil.mcp_server",
    ]


# --- packaging as a standard installable tool (uv tool / pipx / pip) ----------


def test_pyproject_declares_both_console_scripts() -> None:
    """A wheel install must expose BOTH `anvil` and `anvil-mcp`. The `anvil-mcp`
    script is the keystone: without it, every emitted MCP config pointed at the
    bin/anvil-mcp bash wrapper, which a wheel does not ship."""
    scripts = _pyproject()["project"]["scripts"]
    assert scripts.get("anvil") == "anvil.cli:app"
    assert scripts.get("anvil-mcp") == "anvil.mcp_server:main"


def test_pyproject_readme_is_inside_the_build_root() -> None:
    """`readme = "../README.md"` escaped the bin/ build root and broke `uv build`
    (sdist->wheel). Keep the readme path inside bin/ so the release pipeline works."""
    readme = _pyproject()["project"]["readme"]
    assert not str(readme).startswith(".."), "readme must not escape the bin/ build root"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv required to resolve the wheel")
def test_built_wheel_runs_at_declared_dependency_floors(tmp_path: Path) -> None:
    """Keep published floors installable and compatible with Anvil's MCP types."""
    dependencies = _pyproject()["project"]["dependencies"]
    assert "pydantic>=2.11.7" in dependencies
    assert "fastmcp>=3.0.0,<4" in dependencies

    out = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=_repo_root() / "bin",
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert build.returncode == 0, build.stderr[-600:]
    wheel = next(out.glob("*.whl"))

    venv = tmp_path / "resolver-venv"
    created = subprocess.run(
        ["uv", "venv", str(venv), "--python", sys.executable],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert created.returncode == 0, created.stderr[-600:]
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    exact_floor = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            str(wheel),
            "pydantic==2.11.7",
            "fastmcp==3.0.0",
            "pytest>=8,<10",
            "jsonschema>=4,<5",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert exact_floor.returncode == 0, exact_floor.stderr[-1200:]

    dependency_check = subprocess.run(
        ["uv", "pip", "check", "--python", str(python)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert dependency_check.returncode == 0, dependency_check.stderr[-1200:]

    imported = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from anvil import read_contracts; "
                "from anvil.mcp_server import apply_surface_gate, mcp; "
                "assert read_contracts.PROVIDER_LIMITS_V1.max_dependency_edges == 200000; "
                "assert read_contracts.VerificationSummaryV1.model_json_schema(); "
                "assert callable(mcp.enable); assert callable(mcp.disable); "
                "assert hasattr(mcp, '_transforms'); "
                "assert apply_surface_gate(mcp, env={}) is False; "
                "assert apply_surface_gate(mcp, env={'ANVIL_MCP_PLANNING':'1'}) is True"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert imported.returncode == 0, imported.stderr[-1200:]

    mcp_contracts = subprocess.run(
        [
            str(python),
            "-m",
            "pytest",
            "-q",
            "tests/test_mcp.py::TestListTools",
            "tests/test_mcp.py::TestPlanningSurfaceGate",
            "tests/test_mcp.py::TestGetProjectSummary",
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert mcp_contracts.returncode == 0, (
        mcp_contracts.stdout + mcp_contracts.stderr
    )[-1600:]

    public_contracts = subprocess.run(
        [
            str(python),
            "-m",
            "pytest",
            "-q",
            "tests/test_public_read_contracts.py",
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert public_contracts.returncode == 0, (
        public_contracts.stdout + public_contracts.stderr
    )[-2000:]

    below_floor = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--dry-run",
            str(wheel),
            "pydantic==2.11.6",
            "fastmcp==3.0.0",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert below_floor.returncode != 0
    assert "pydantic>=2.11.7" in (below_floor.stdout + below_floor.stderr)

    below_fastmcp_floor = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--dry-run",
            str(wheel),
            "pydantic==2.11.7",
            "fastmcp==2.14.7",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert below_fastmcp_floor.returncode != 0
    fastmcp_refusal = below_fastmcp_floor.stdout + below_fastmcp_floor.stderr
    assert "fastmcp" in fastmcp_refusal
    assert ">=3.0.0" in fastmcp_refusal


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv required to build the wheel")
def test_built_wheel_is_self_sufficient(tmp_path: Path) -> None:
    """End-to-end guard for the regression the audit found: a pip/uv-tool install
    must be self-sufficient. Build the wheel and assert it (a) builds at all (the
    readme/sdist fix), (b) ships AGENTS.md + codex automations as package data, and
    (c) declares the anvil-mcp entry point. Skips if the build backend is
    unavailable in this env, but FAILS loudly if the readme path bug returns."""
    out = tmp_path / "dist"
    r = subprocess.run(
        ["uv", "build", "--out-dir", str(out)],
        cwd=_repo_root() / "bin",
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        if "readme" in r.stderr.lower() or "README" in r.stderr:
            pytest.fail(f"build broke on the readme path bug again:\n{r.stderr[-600:]}")
        pytest.skip(f"wheel build unavailable in this env:\n{r.stderr[-300:]}")
    assert list(out.glob("*.tar.gz")), "sdist not built"
    wheels = list(out.glob("*.whl"))
    assert wheels, "wheel not built"
    with zipfile.ZipFile(wheels[0]) as z:
        names = set(z.namelist())
        eps = [n for n in names if n.endswith("entry_points.txt")]
        assert eps, "wheel ships no entry_points.txt"
        entry = z.read(eps[0]).decode()
    assert "anvil/_data/AGENTS.md" in names, "AGENTS.md not shipped as package data"
    assert any(
        n.startswith("anvil/_data/packaging/codex/automations/") for n in names
    ), "codex automation templates not shipped as package data"
    assert "anvil-mcp = anvil.mcp_server:main" in entry


# --- codex: plugin.json (VERIFIED) ---------------------------------------


def test_root_mcp_json_launches_without_shell_wrapper() -> None:
    """The root plugin MCP manifest must not depend on a bare ``bash`` command."""
    p = _repo_root() / ".mcp.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    spec = data["mcpServers"]["anvil"]
    assert spec["type"] == "stdio"
    _assert_uv_mcp_spec(spec, "CLAUDE_PLUGIN_ROOT")


def test_codex_plugin_json_parses_and_has_fields() -> None:
    p = _packaging() / "codex" / ".codex-plugin" / "plugin.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    for field in ("name", "version", "description", "license", "skills",
                  "mcpServers", "interface"):
        assert field in data, f"codex plugin.json missing {field!r}"
    assert data["name"] == "anvil"
    # The Codex validator rejects `hooks` — it must be ABSENT.
    assert "hooks" not in data
    # mcpServers points at the bundled .mcp.json.
    assert data["mcpServers"] == "./.mcp.json"


def test_codex_plugin_version_matches_anvil_version() -> None:
    """plugin.json version is synced to anvil.__version__ (reuse the
    test_version_sync.py spirit)."""
    import anvil

    p = _packaging() / "codex" / ".codex-plugin" / "plugin.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["version"] == anvil.__version__, (
        f"codex plugin.json version {data['version']!r} != "
        f"anvil.__version__ {anvil.__version__!r} — keep them synced."
    )


def test_codex_mcp_json_matches_codex_envelope() -> None:
    """The bundled .mcp.json launches the MCP server without a shell wrapper."""
    p = _packaging() / "codex" / ".mcp.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    spec = data["mcpServers"]["anvil"]
    _assert_uv_mcp_spec(spec, "CLAUDE_PLUGIN_ROOT")
    assert spec["cwd"] == "${CLAUDE_PLUGIN_ROOT}"
    assert "CODEX_PLUGIN_ROOT" not in json.dumps(spec)


def test_codex_hooks_json_has_no_top_level_metadata() -> None:
    """Codex's hook loader rejects unknown top-level keys.

    Keep the shipped plugin hook manifest to the strict runtime shape so fresh
    Codex installs do not fail with "unknown field `description`, expected
    `hooks`".
    """
    p = _repo_root() / "hooks" / "hooks.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert set(data) == {"hooks"}
    assert isinstance(data["hooks"], dict)


def test_codex_hooks_json_uses_shell_free_dispatcher() -> None:
    """Codex runs plugin hooks on Windows too; the manifest must not rely on bash."""
    p = _repo_root() / "hooks" / "hooks.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "Stop" not in data["hooks"], "blocking stop-gate must remain opt-in"
    commands = [
        hook["command"]
        for event_specs in data["hooks"].values()
        for event_spec in event_specs
        for hook in event_spec["hooks"]
    ]
    assert commands, "expected hook commands"
    expected_prefix = 'uv run --quiet --project "${CLAUDE_PLUGIN_ROOT}/bin" '
    expected = [
        expected_prefix + "python -m anvil.cli hook dispatch detect-state",
        expected_prefix + "python -m anvil.cli hook dispatch check-claim",
        expected_prefix + "python -m anvil.cli hook dispatch record-file-change",
        expected_prefix + "python -m anvil.cli hook dispatch capture-evidence",
        expected_prefix + "python -m anvil.cli hook dispatch heartbeat",
        expected_prefix + "python -m anvil.cli hook dispatch heartbeat",
    ]
    assert Counter(commands) == Counter(expected)
    for command in commands:
        lowered = command.lower()
        assert "bash" not in lowered
        assert "python3" not in lowered
        assert "jq" not in lowered
        assert "powershell" not in lowered
        assert "pwsh" not in lowered
        assert "cmd.exe" not in lowered
        assert "&&" not in command and ";" not in command and "|" not in command


# --- codex: marketplace.json (VERIFIED) ----------------------------------


def test_codex_marketplace_json_parses_one_plugin() -> None:
    p = _packaging() / "codex" / ".agents" / "plugins" / "marketplace.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    plugins = data["plugins"]
    assert len(plugins) == 1
    entry = plugins[0]
    assert entry["name"] == "anvil"
    assert entry["source"]["source"] == "local"
    assert entry["policy"]["installation"] == "AVAILABLE"
    assert entry["policy"]["authentication"] == "ON_USE"


def test_codex_marketplace_version_synced() -> None:
    import anvil

    p = _packaging() / "codex" / ".agents" / "plugins" / "marketplace.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["plugins"][0]["version"] == anvil.__version__


# --- gemini: gemini-extension.json (VERIFIED) ----------------------------


def test_gemini_extension_json() -> None:
    p = _packaging() / "gemini" / "gemini-extension.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["name"] == "anvil"
    # contextFileName points at AGENTS.md ITSELF (not a copy) → drift-guard
    # asserts the field value, not a file copy (see test_install_drift.py).
    assert data["contextFileName"] == "AGENTS.md"
    assert "anvil" in data["mcpServers"]
    spec = data["mcpServers"]["anvil"]
    assert spec["command"] == "bash"
    # Uses Gemini's ${extensionPath} substitution — portable inside the ext dir.
    assert any("${extensionPath}" in a for a in spec["args"])


def test_gemini_version_synced() -> None:
    import anvil

    p = _packaging() / "gemini" / "gemini-extension.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["version"] == anvil.__version__


# --- openclaw: README + version-locked native plugin (VERIFIED) ----------


def test_openclaw_readme_exists() -> None:
    p = _packaging() / "openclaw" / "README.md"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    # Documents that hooks are detected-but-not-executed.
    assert "hooks" in text.lower()


def test_openclaw_plugin_version_synced() -> None:
    """The native OpenClaw plugin manifest is version-locked to anvil (T001) —
    kept in lockstep so its declared version never drifts (CI catches it), the
    same enforced-consistency discipline as the codex/gemini manifests. The field
    itself is informational (OpenClaw reloads the plugin via `--link` + gateway
    restart, not the version string), but shipping a stale one — it was `0.0.1` —
    is still wrong."""
    import anvil

    p = _packaging() / "openclaw" / "plugin" / "openclaw.plugin.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["version"] == anvil.__version__, (
        f"openclaw.plugin.json version {data['version']!r} != "
        f"anvil.__version__ {anvil.__version__!r} — keep them synced."
    )


def test_openclaw_package_version_synced() -> None:
    import anvil

    p = _packaging() / "openclaw" / "plugin" / "package.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["version"] == anvil.__version__, (
        f"openclaw package.json version {data['version']!r} != "
        f"anvil.__version__ {anvil.__version__!r} — keep them synced."
    )


# --- STUBs (NOT verified) ------------------------------------------------


@pytest.mark.parametrize("harness", ["cline"])
def test_stub_has_todo_and_no_manifest_body(harness: str) -> None:
    """STUBs must contain TODO and carry NO parseable JSON/TOML manifest body —
    guard against a stub silently becoming a guessed manifest."""
    p = _packaging() / harness / "STUB.md"
    assert p.is_file(), f"missing STUB for {harness}"
    text = p.read_text(encoding="utf-8")
    assert "TODO" in text, f"{harness} STUB.md must name what to verify (TODO)"

    # No JSON manifest body: a fenced ```json block, or a bare {...} object that
    # parses as a dict, would mean a guessed manifest leaked into the stub.
    assert "```json" not in text
    assert "```toml" not in text
    # A `{` followed later by a `}` that json.loads accepts as a dict is banned.
    if "{" in text and "}" in text:
        snippet = text[text.index("{"): text.rindex("}") + 1]
        try:
            parsed = json.loads(snippet)
        except json.JSONDecodeError:
            parsed = None
        assert not isinstance(parsed, dict), (
            f"{harness} STUB.md contains a parseable JSON object — that looks "
            "like a guessed manifest. STUBs must stay manifest-free."
        )


# --- openhands: config snippet (VERIFIED) ----------------------------------
# Instruction file is the project-root AGENTS.md (current OpenHands convention);
# the .openhands/microagents/ path is deprecated V0 and no longer shipped.


def test_openhands_config_snippet_has_stdio_servers() -> None:
    """The config.toml snippet uses the correct OpenHands TOML key.

    Format confirmed from OpenHands config.template.toml: [mcp] table with
    stdio_servers array of inline tables {name, command, args[, env]}.
    """
    p = _packaging() / "openhands" / "config.toml.snippet"
    assert p.is_file(), "missing packaging/openhands/config.toml.snippet"
    text = p.read_text(encoding="utf-8")

    assert "[mcp]" in text, "snippet must contain [mcp] table header"
    assert "stdio_servers" in text, "snippet must use stdio_servers key"
    assert "anvil" in text, "snippet must reference the anvil server name"


# --- opencode: opencode.json (VERIFIED) ------------------------------------


def test_opencode_manifest_has_mcp_anvil() -> None:
    """The committed opencode.json reference parses and carries the anvil server.

    OpenCode shape (confirmed from opencode.ai/config schema): mcp.anvil with
    type 'local', an argv-array command ending in bin/anvil-mcp, enabled true.
    """
    p = _packaging() / "opencode" / "opencode.json"
    assert p.is_file(), "missing packaging/opencode/opencode.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data.get("$schema") == "https://opencode.ai/config.json"
    spec = data["mcp"]["anvil"]
    assert spec["type"] == "local"
    assert spec["enabled"] is True
    assert isinstance(spec["command"], list)
    assert spec["command"][-1].endswith("bin/anvil-mcp")


# --- roo / amp / continue / goose committed references (VERIFIED) -----------


def test_roo_manifest_has_mcp_servers() -> None:
    p = _packaging() / "roo" / ".roo" / "mcp.json"
    assert p.is_file(), "missing packaging/roo/.roo/mcp.json"
    spec = json.loads(p.read_text(encoding="utf-8"))["mcpServers"]["anvil"]
    assert spec["args"][-1].endswith("bin/anvil-mcp")


def test_amp_manifest_uses_flat_dotted_key() -> None:
    p = _packaging() / "amp" / "settings.json"
    assert p.is_file(), "missing packaging/amp/settings.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "amp.mcpServers" in data  # flat dotted key, not nested
    assert data["amp.mcpServers"]["anvil"]["args"][-1].endswith("bin/anvil-mcp")


def test_continue_manifest_is_valid_yaml_block() -> None:
    p = _packaging() / "continue" / ".continue" / "mcpServers" / "anvil.yaml"
    assert p.is_file(), "missing packaging/continue/.continue/mcpServers/anvil.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert doc["schema"] == "v1"
    anvil_srv = next(s for s in doc["mcpServers"] if s["name"] == "anvil")
    assert anvil_srv["command"] == "bash"


def test_goose_manifest_has_stdio_extension() -> None:
    p = _packaging() / "goose" / "config.yaml"
    assert p.is_file(), "missing packaging/goose/config.yaml"
    ext = yaml.safe_load(p.read_text(encoding="utf-8"))["extensions"]["anvil"]
    assert ext["type"] == "stdio"
    assert ext["cmd"] == "bash"  # goose uses cmd, not command
    assert ext["enabled"] is True
