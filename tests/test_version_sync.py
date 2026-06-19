"""Regression test for version-string sync across anvil's three
Python source-of-truth files. Added after structure-critic MUST FIX on
PR #65 caught `__init__.py` stale at 1.16.0 while every other source
was at 1.17.0.

The three sources that MUST agree (the Python world):

  1. ``bin/pyproject.toml`` — what pip / uv reads at install
  2. ``bin/src/anvil/__init__.py`` — what ``import anvil``
     exposes as ``__version__`` at runtime
  3. ``.claude-plugin/plugin.json`` — what Claude Code's plugin loader
     reads at install/load

``.claude-plugin/marketplace.json`` deliberately omits ``version`` so it
inherits from ``plugin.json`` — there is no fourth version string to keep in
sync (its schema/wiring is guarded by ``tests/test_marketplace.py``). README
badges are documentation, not a source of truth — also not checked here.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path


def _plugin_root() -> Path:
    """Return the absolute path of the anvil plugin directory.

    The test file lives at ``plugins/anvil/tests/test_version_sync.py``,
    so ``parents[1]`` is the plugin root.
    """
    return Path(__file__).resolve().parents[1]


def test_version_sync_across_pyproject_initpy_pluginjson() -> None:
    """All three Python source-of-truth version strings MUST match."""
    plugin = _plugin_root()

    # 1. pyproject.toml — read via tomllib (stdlib in 3.11+).
    pyproject = plugin / "bin" / "pyproject.toml"
    with pyproject.open("rb") as fh:
        py_version = tomllib.load(fh)["project"]["version"]

    # 2. __init__.py — import directly. This works because the test runner
    #    has `bin/src` on the path (via the editable install or
    #    pyproject's `tool.hatch.build.targets.wheel.packages` config).
    import anvil

    init_version = anvil.__version__

    # 3. plugin.json — read via stdlib json.
    plugin_json_path = plugin / ".claude-plugin" / "plugin.json"
    with plugin_json_path.open(encoding="utf-8") as fh:
        manifest_version = json.load(fh)["version"]

    # All three MUST agree. The error message names every source so a
    # release manager can fix the lagging file without grepping.
    assert py_version == init_version == manifest_version, (
        f"Version drift across anvil sources of truth:\n"
        f"  bin/pyproject.toml             → {py_version}\n"
        f"  anvil/__init__.py       → {init_version}\n"
        f"  .claude-plugin/plugin.json     → {manifest_version}\n"
        f"All three MUST match. (regression test for "
        f"structure-critic MUST FIX, PR #65)"
    )


def test_version_is_semver_shaped() -> None:
    """Sanity-check the shape is N.N.N (no prerelease suffix in main).

    Catches accidental edits like `1.17` (missing patch) or `1.17.0-dev`
    (leftover prerelease) before they reach the marketplace.
    """
    import anvil

    parts = anvil.__version__.split(".")
    assert len(parts) == 3, (
        f"Expected MAJOR.MINOR.PATCH; got {anvil.__version__!r}"
    )
    for part in parts:
        assert part.isdigit(), (
            f"Each version component must be all digits; "
            f"got {part!r} in {anvil.__version__!r}"
        )
