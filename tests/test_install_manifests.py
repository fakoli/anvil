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

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from collections import Counter
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from anvil.cli.describe import build_manifest
from anvil.state.schema import SCHEMA_VERSION


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _packaging() -> Path:
    return _repo_root() / "packaging"


def _upgrade_guide() -> str:
    return (_repo_root() / "docs" / "how-to" / "getting-started.md").read_text(
        encoding="utf-8"
    )


def _migration_guide() -> str:
    return (_repo_root() / "docs" / "migrations.md").read_text(encoding="utf-8")


def _cli_reference() -> str:
    return (_repo_root() / "docs" / "cli-reference.md").read_text(encoding="utf-8")


def _faq() -> str:
    return (_repo_root() / "docs" / "faq.md").read_text(encoding="utf-8")


def _pyproject() -> dict:
    return tomllib.loads((_repo_root() / "bin" / "pyproject.toml").read_text(encoding="utf-8"))


def _provider_resource(relative: str) -> bytes:
    return files("anvil._data").joinpath(relative).read_bytes()


def _provider_document(relative: str) -> dict:
    return json.loads(_provider_resource(relative))


def test_described_provider_schemas_and_fixtures_are_packaged_and_canonical() -> None:
    catalog = build_manifest()["operation_catalog"]
    advertised = [
        resource
        for operation in catalog["operations"]
        for group in ("schema_resources", "fixture_resources")
        for resource in operation[group].values()
    ]
    assert len(set(advertised)) < len(advertised), "shared resources should be reused"

    for relative in sorted(set(advertised)):
        raw = _provider_resource(relative)
        document = json.loads(raw)
        if relative.endswith(".schema.json"):
            rendered = json.dumps(
                document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
        else:
            rendered = json.dumps(
                document, sort_keys=True, indent=2, ensure_ascii=True
            )
        canonical_fixture = (rendered + "\n").encode("ascii")
        assert raw == canonical_fixture, relative

    for operation in catalog["operations"]:
        schemas = operation["schema_resources"]
        fixtures = operation["fixture_resources"]
        for kind in ("input", "output", "error"):
            schema = _provider_document(schemas[kind])
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(_provider_document(fixtures[kind]))


def test_provider_schemas_reject_unknown_and_incompatible_wire_fields() -> None:
    for operation in build_manifest()["operation_catalog"]["operations"]:
        schemas = operation["schema_resources"]
        fixtures = operation["fixture_resources"]
        for kind in ("input", "output"):
            schema = _provider_document(schemas[kind])
            fixture = _provider_document(fixtures[kind])
            validator = Draft202012Validator(schema)

            unknown = {**fixture, "unknown_contract_field": True}
            assert list(validator.iter_errors(unknown)), (operation, kind)

            incompatible = {**fixture, "operation_version": 2}
            assert list(validator.iter_errors(incompatible)), (operation, kind)


def test_packaged_provider_digest_vectors_are_exact() -> None:
    fixture = _provider_document(
        "contracts/provider-reads/v1/provider-read-digests.v1.json"
    )
    snapshot = fixture["project_snapshot"]
    snapshot_digest = hashlib.sha256(
        base64.b64decode(snapshot["domain_base64"])
        + base64.b64decode(snapshot["canonical_payload_utf8_base64"])
    ).hexdigest()
    assert snapshot_digest == snapshot["snapshot_digest"]

    content = fixture["prd_content"]
    domain = base64.b64decode(content["domain_base64"])
    source = base64.b64decode(content["source_utf8_base64"])
    source_digest = hashlib.sha256(source).hexdigest()
    assert source_digest == content["source_digest"]

    def digest(selector: dict, returned: bytes) -> str:
        selector_bytes = json.dumps(
            selector, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(
            domain
            + source_digest.encode("ascii")
            + b"\0"
            + selector_bytes
            + b"\0"
            + returned
        ).hexdigest()

    assert content["full_content_digest"] == (
        "f5e22d65e4df03b1f3a935f3aa049831d0399b87d04196dbacc48633354af72d"
    )
    assert digest(content["full_selector"], source) == content["full_content_digest"]
    summary = base64.b64decode(content["summary_utf8_base64"])
    assert content["summary_content_digest"] == (
        "c7418b66a23c89794c79a02c9f325f100ddc0534659d666297afa26a7a60a99c"
    )
    assert digest(content["summary_selector"], summary) == (
        content["summary_content_digest"]
    )


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


def test_upgrade_guide_covers_every_version_boundary() -> None:
    """Keep issue #180's recovery runbook executable and transport-complete."""
    guide = _upgrade_guide()
    for required in (
        "command -v anvil",
        "Get-Command anvil, anvil-mcp",
        "anvil status --path-only",
        "anvil --version",
        "db_schema_version",
        "pre_open_database_schema",
        "supported_schema",
        "database_schema",
        'cp -a "$STATE_DIR" "$BACKUP_DIR"',
        "Copy-Item -Recurse -LiteralPath $stateDir",
        "uv tool upgrade anvil-state",
        "claude plugin marketplace update anvil",
        "claude plugin update anvil@anvil",
        "codex plugin marketplace upgrade anvil",
        "anvil install codex --write",
        "anvil install openclaw --write",
        "anvil mcp-config <client>",
        '"method":"initialize"',
        "serverInfo",
    ):
        assert required in guide
    upgrade_section = guide.split("## Upgrading and uninstalling", 1)[1]
    assert "/plugin install" not in upgrade_section


def test_upgrade_guide_orders_restart_before_live_mcp_verification() -> None:
    """A disk upgrade is incomplete until a fresh harness process is checked."""
    guide = _upgrade_guide()
    ordered_steps = (
        "Upgrade the Python CLI/MCP install; this does not open project state.",
        "With the upgraded CLI, resolve the state path and make the backup.",
        "Refresh the plugin or harness integration",
        "Fully restart every harness and MCP server process.",
        "Verify the live MCP initialize metadata below.",
    )
    offsets = [guide.index(step) for step in ordered_steps]
    assert offsets == sorted(offsets)
    assert guide.index("anvil --version") < guide.index("uv tool upgrade anvil-state")
    assert guide.index("uv tool upgrade anvil-state") < guide.index(
        "anvil status --path-only"
    )
    assert guide.index("anvil status --path-only") < guide.index(
        'cp -a "$STATE_DIR" "$BACKUP_DIR"'
    ) < guide.index("STATUS_JSON=$(anvil status --json || true)")
    assert guide.index("$stateDir = anvil status --path-only") < guide.index(
        "Copy-Item -Recurse -LiteralPath $stateDir"
    ) < guide.index("$status = anvil status --json | ConvertFrom-Json")
    upgrade_section = guide.split("## Upgrading and uninstalling", 1)[1]
    bash_backup = upgrade_section.index('cp -a "$STATE_DIR" "$BACKUP_DIR"')
    powershell_backup = upgrade_section.index(
        "Copy-Item -Recurse -LiteralPath $stateDir"
    )
    explicit_migration = upgrade_section.index("anvil migrate state        # dry run")
    ordinary_status = upgrade_section.index("STATUS_JSON=$(anvil status --json || true)")
    assert max(bash_backup, powershell_backup) < explicit_migration < ordinary_status
    normalized = " ".join(guide.split())
    assert "database stamp observed before the backend opens" in normalized
    assert "rerun the status block to confirm the values are now equal" in normalized
    assert "do not delete `state.db` or the state directory" in normalized
    assert "Routine version recovery never requires deleting state" in normalized


def test_upgrade_references_match_current_schema_and_path_only_contract() -> None:
    """Operator references must track the implementation's live version boundary."""
    migrations = _migration_guide()
    cli_reference = _cli_reference()
    faq = _faq()
    assert f"currently v{SCHEMA_VERSION}" in migrations
    assert f"v3 -> v{SCHEMA_VERSION}" in migrations
    assert f"v{SCHEMA_VERSION - 1}→v{SCHEMA_VERSION}" in migrations
    assert "--path-only" in cli_reference
    assert "without opening" in cli_reference
    assert "uninitialised project" in cli_reference
    assert f"v{SCHEMA_VERSION} in\nthis release" in faq
    assert "The schema is version 8" not in faq
    assert faq.count("STATE_DIR=$(anvil status --path-only)") >= 3
    assert "anvil status | grep '^Path:'" not in faq
    assert "normal state command first initializes the backend" in migrations
    assert "default dry-run of `anvil migrate state`" in migrations
    assert "deliberately do not initialize and\nmigrate the backend" in migrations
    assert f"Schema:        {SCHEMA_VERSION}" in _upgrade_guide()
    assert "Schema:        8" not in _upgrade_guide()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell not installed")
def test_powershell_upgrade_block_flushes_heterogeneous_tables() -> None:
    """The pasted block must render schema fields after its executable table."""
    guide = _upgrade_guide()
    assert (
        "Get-Command anvil, anvil-mcp | Select-Object Name, Source | Out-Host"
        in guide
    )
    script = """
Get-Command pwsh | Select-Object Name, Source | Out-Host
[pscustomobject]@{
    status = 'compatible'
    engine_schema = 16
    pre_open_database_schema = 0
}
"""
    rendered = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert "compatible" in rendered.stdout
    assert "pre_open_database_schema" in rendered.stdout
    assert "16" in rendered.stdout
    assert "0" in rendered.stdout


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
    provider_resources = {
        resource
        for operation in build_manifest()["operation_catalog"]["operations"]
        for group in ("schema_resources", "fixture_resources")
        for resource in operation[group].values()
    }
    for resource in provider_resources:
        archived = f"anvil/_data/{resource}"
        assert archived in names, f"provider contract resource not shipped: {archived}"
        assert sum(name == archived for name in names) == 1
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


def test_openclaw_actor_precedence_matches_cli() -> None:
    source = (
        _packaging() / "openclaw" / "plugin" / "index.ts"
    ).read_text(encoding="utf-8")
    assert (
        "process.env.ANVIL_ACTOR ?? process.env.ANVIL_GATE_ACTOR ?? \"agent\""
        in source
    )


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
