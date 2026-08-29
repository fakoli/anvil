"""Guard the root ``plugin.json`` — the portable Agent Plugins v1 manifest that
lets Agent-Plugins-compatible clients (e.g. Hermes) discover this repo.

Why this exists: the portable manifest is a *fourth* identity file alongside
``.claude-plugin/plugin.json``, ``packaging/codex/.codex-plugin/plugin.json``
and ``packaging/openclaw/plugin/openclaw.plugin.json``. Nothing validated it,
and the v1 schema is stricter than Claude's: it sets ``additionalProperties:
false``, so a stray field is not merely untidy — a strict client rejects the
whole manifest over it. (Hermes happens to warn-and-strip rather than reject,
which is why a broken field can ship green.)

These are schema/wiring assertions, not engine logic. They enforce the two
things that actually break portable discovery:

  1. The manifest validates against the canonical v1 JSON Schema, and loads
     through Hermes' own portable reader with zero diagnostics.
  2. ``skills/`` is a real in-root directory of Agent Skills. Component
     locations are fixed by the spec — a manifest field cannot point them
     elsewhere, which is why ``skills`` is deliberately NOT a manifest key.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JSON = REPO_ROOT / "plugin.json"

# Canonical, version-pinned. Matches PLUGIN_SCHEMA_V1 in Hermes' reader.
PLUGIN_SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"

# v1 allows EXACTLY these top-level fields (schema additionalProperties:false,
# mirrored as _PLUGIN_FIELDS in hermes_cli/agent_plugins.py). Kept inline rather
# than fetched so the test is hermetic and cannot pass by network fluke.
V1_FIELDS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}

# Claude Code's manifest shape. Valid for .claude-plugin/plugin.json, invalid
# for a v1 manifest — its presence is the exact mistake this guards.
CLAUDE_ONLY_FIELDS = {"interface", "mcpServers", "commands", "agents", "hooks"}


def _manifest() -> dict:
    return json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))


def test_plugin_manifest_has_required_v1_identity() -> None:
    doc = _manifest()
    assert doc.get("$schema") == PLUGIN_SCHEMA_URL, (
        "plugin.json must declare the canonical Agent Plugins v1 schema URL; "
        "a missing or different value makes portable clients reject it outright"
    )
    name = doc.get("name")
    assert isinstance(name, str) and name, "plugin.json needs a name"
    assert re.fullmatch(r"(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", name), (
        f"plugin name {name!r} violates the v1 name pattern"
    )
    assert name == "anvil", "portable plugin name must stay 'anvil'"


def test_plugin_manifest_carries_no_fields_outside_v1() -> None:
    extra = sorted(set(_manifest()) - V1_FIELDS)
    assert not extra, (
        f"plugin.json has fields outside Agent Plugins v1: {extra}. v1 sets "
        "additionalProperties:false, so a strict client rejects the manifest. "
        "Move client-specific data under 'extensions' with a reverse-domain key."
    )


def test_plugin_manifest_validates_against_canonical_schema() -> None:
    # Inline schema mirror of the v1 field set, applied as additionalProperties
    # is enforced by the real schema. Draft202012Validator is already used by
    # tests/test_project_snapshot.py, so this adds no new dependency.
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {k: {} for k in sorted(V1_FIELDS)},
        "required": ["$schema", "name"],
        "additionalProperties": False,
    }
    Draft202012Validator(schema).validate(_manifest())


def test_plugin_manifest_does_not_smuggle_claude_shape() -> None:
    smuggled = sorted(set(_manifest()) & CLAUDE_ONLY_FIELDS)
    assert not smuggled, (
        f"plugin.json carries Claude-only field(s) {smuggled}. Those belong in "
        ".claude-plugin/plugin.json; the portable manifest must stay v1-clean."
    )


def test_portable_skills_are_a_real_in_root_directory() -> None:
    skills = REPO_ROOT / "skills"
    assert skills.is_dir(), "skills/ missing — a portable package ships no components"
    assert (skills / "SKILL.md").is_file() or any(
        (d / "SKILL.md").is_file() for d in skills.iterdir() if d.is_dir()
    ), "no SKILL.md anywhere under skills/ — the package would load zero skills"


def test_version_matches_the_canonical_sources() -> None:
    """The root manifest is a version-bearing file: keep it in sync.

    tests/test_version_sync.py guards bin/pyproject.toml, __init__.py,
    .claude-plugin/plugin.json and uv.lock. This closes the gap for the
    portable manifest so a release bump cannot leave it stale.
    """
    init_py = (REPO_ROOT / "bin" / "src" / "anvil" / "__init__.py").read_text(
        encoding="utf-8"
    )
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', init_py, re.MULTILINE)
    assert m, "could not read __version__ from bin/src/anvil/__init__.py"
    assert _manifest().get("version") == m.group(1), (
        f"plugin.json version {_manifest().get('version')!r} != package "
        f"version {m.group(1)!r} — bump plugin.json in the release commit"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
