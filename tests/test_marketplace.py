"""Guard the root ``.claude-plugin/marketplace.json`` so a broken catalog can
never ship silently — the exact failure that made ``/plugin marketplace add
fakoli/anvil`` do nothing (the manifest was left behind during the monorepo
extraction).

These are schema/wiring assertions, not engine logic: marketplace.json is a
catalog Claude Code reads at ``/plugin marketplace add``. The test enforces the
two things that actually break installation if wrong:

  1. The catalog is valid JSON with the required top-level fields.
  2. The single plugin entry's ``source`` resolves to a directory that holds a
     real ``.claude-plugin/plugin.json``, and the entry's ``name`` matches that
     manifest — otherwise the documented ``/plugin install anvil@anvil`` breaks.
"""

from __future__ import annotations

import json
from pathlib import Path


def _repo_root() -> Path:
    # tests/ sits at the repo root, so parents[1] is the root (mirrors
    # test_version_sync.py).
    return Path(__file__).resolve().parents[1]


def test_marketplace_manifest_is_valid_and_wired() -> None:
    root = _repo_root()
    mp_path = root / ".claude-plugin" / "marketplace.json"
    assert mp_path.is_file(), "marketplace.json missing — repo won't add as a marketplace"

    mp = json.loads(mp_path.read_text(encoding="utf-8"))

    # Required top-level fields (Claude Code marketplace schema).
    assert isinstance(mp.get("name"), str) and mp["name"], "marketplace needs a name"
    assert isinstance(mp.get("owner"), dict) and mp["owner"].get("name"), "owner.name required"
    plugins = mp.get("plugins")
    assert isinstance(plugins, list) and plugins, "marketplace lists no plugins"

    # The anvil plugin entry must resolve to a real plugin manifest, and its
    # name must match so `/plugin install <plugin>@<marketplace>` works.
    pj = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    entry = next((p for p in plugins if p.get("name") == pj["name"]), None)
    assert entry is not None, f"no marketplace entry named {pj['name']!r} (matches plugin.json)"

    source = entry.get("source")
    assert isinstance(source, str) and source.startswith("./"), (
        "relative plugin source must start with './' and resolve from repo root"
    )
    target = (root / source).resolve() / ".claude-plugin" / "plugin.json"
    assert target.is_file(), f"source {source!r} does not resolve to a plugin.json ({target})"
