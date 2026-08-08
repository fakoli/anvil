"""Regression test for version-string sync across anvil's Python
version-bearing files. Added after structure-critic MUST FIX on
PR #65 caught `__init__.py` stale at 1.16.0 while every other source
was at 1.17.0.

The Python package/runtime version-bearing files that MUST agree:

  1. ``bin/pyproject.toml`` — what pip / uv reads at install
  2. ``bin/src/anvil/__init__.py`` — what ``import anvil``
     exposes as ``__version__`` at runtime
  3. ``.claude-plugin/plugin.json`` — what Claude Code's plugin loader
     reads at install/load
  4. ``bin/uv.lock`` — committed editable-package lock metadata

``.claude-plugin/marketplace.json`` deliberately omits ``version`` so it
inherits from ``plugin.json`` — there is no extra root Claude marketplace
version string to keep in sync (its schema/wiring is guarded by
``tests/test_marketplace.py``). README badges are documentation, not a source of
truth — also not checked here.
"""

from __future__ import annotations

import json
import re
import runpy
import subprocess
import tomllib
from pathlib import Path

import pytest

from anvil.build_identity import (
    _checkout_root,
    _embedded_identity,
    _run_git,
    _source_identity,
)


def _plugin_root() -> Path:
    """Return the absolute path of the anvil plugin directory.

    The test file lives at ``plugins/anvil/tests/test_version_sync.py``,
    so ``parents[1]`` is the plugin root.
    """
    return Path(__file__).resolve().parents[1]


def test_version_sync_across_pyproject_initpy_pluginjson_lockfile() -> None:
    """All Python package/runtime version strings MUST match."""
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

    # 4. uv.lock — the editable package entry must track pyproject. Other
    #    dependency versions are unrelated and can legitimately differ.
    uv_lock_path = plugin / "bin" / "uv.lock"
    with uv_lock_path.open("rb") as fh:
        lock_data = tomllib.load(fh)
    lock_version = next(
        package["version"]
        for package in lock_data["package"]
        if package["name"] == "anvil-state"
        and package.get("source", {}).get("editable") == "."
    )

    # All four MUST agree. The error message names every source so a
    # release manager can fix the lagging file without grepping.
    assert py_version == init_version == manifest_version == lock_version, (
        f"Version drift across anvil sources of truth:\n"
        f"  bin/pyproject.toml             → {py_version}\n"
        f"  anvil/__init__.py              → {init_version}\n"
        f"  .claude-plugin/plugin.json     → {manifest_version}\n"
        f"  bin/uv.lock editable package   → {lock_version}\n"
        f"All four MUST match. (regression test for "
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


def test_current_documentation_examples_match_package_version() -> None:
    """Current-behavior examples must move with every release bump."""
    import anvil

    root = _plugin_root()
    schema_examples: list[str] = []
    for relative in (
        "docs/how-to/getting-started.md",
        "docs/cli-reference.md",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        schema_examples.extend(
            re.findall(r"anvil (\d+\.\d+\.\d+) \(schema \d+\)", text)
        )
    assert schema_examples
    assert set(schema_examples) == {anvil.__version__}

    mcp = (root / "docs" / "mcp.md").read_text(encoding="utf-8")
    engine_examples = re.findall(
        r'"engine_version"\s*:\s*"(\d+\.\d+\.\d+)"', mcp
    )
    display_examples = re.findall(
        r'"display_version"\s*:\s*"(\d+\.\d+\.\d+)"', mcp
    )
    tag_examples = re.findall(r'"tag"\s*:\s*"v(\d+\.\d+\.\d+)"', mcp)
    assert engine_examples and display_examples and tag_examples
    assert set(engine_examples + display_examples + tag_examples) == {
        anvil.__version__
    }


def test_exact_release_checkout_remains_visibly_source_derived(tmp_path: Path) -> None:
    def run_git(root: Path, *args: str) -> str | None:
        assert root == tmp_path
        if args == ("rev-parse", "--show-toplevel"):
            return str(tmp_path)
        assert args[0] == "describe"
        return "v0.6.1-0-gabcdef1"

    identity = _source_identity("0.6.1", tmp_path, run_git=run_git)

    assert identity.build_kind == "source_checkout"
    assert identity.display_version == "0.6.1+0.gabcdef1"
    assert identity.commit == "abcdef1"
    assert identity.tag == "v0.6.1"
    assert identity.tag_distance == 0
    assert identity.dirty is False


def test_ahead_or_dirty_checkout_has_visible_bounded_provenance(
    tmp_path: Path,
) -> None:
    def run_git(root: Path, *args: str) -> str | None:
        assert root == tmp_path
        if args == ("rev-parse", "--show-toplevel"):
            return str(tmp_path)
        assert args[0] == "describe"
        return "v0.6.0-29-g9FE02C0-dirty"

    identity = _source_identity("0.6.1", tmp_path, run_git=run_git)

    assert identity.build_kind == "source_checkout"
    assert identity.display_version == "0.6.1+29.g9fe02c0.dirty"
    assert identity.commit == "9fe02c0"
    assert identity.tag_distance == 29
    assert identity.dirty is True


def test_source_without_describe_metadata_never_claims_exact_release(
    tmp_path: Path,
) -> None:
    def no_git_metadata(root: Path, *args: str) -> str | None:
        assert root == tmp_path
        return None

    identity = _source_identity("0.6.1", tmp_path, run_git=no_git_metadata)

    assert identity.build_kind == "source_unknown"
    assert identity.display_version == "0.6.1+source.unknown"
    assert identity.commit is None


def test_source_archive_nested_in_another_repo_ignores_parent_git(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "extracted-anvil"

    def parent_repo(root: Path, *args: str) -> str | None:
        assert root == archive
        assert args == ("rev-parse", "--show-toplevel")
        return str(tmp_path)

    identity = _source_identity("0.6.1", archive, run_git=parent_repo)

    assert identity.build_kind == "source_unknown"
    assert identity.commit is None


def test_checkout_detection_requires_anvil_source_layout(tmp_path: Path) -> None:
    package = tmp_path / "bin" / "src" / "anvil" / "build_identity.py"
    package.parent.mkdir(parents=True)
    package.write_text("", encoding="utf-8")
    (tmp_path / "bin" / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    assert _checkout_root(package) == tmp_path


def test_repo_local_virtualenv_is_not_mistaken_for_source(tmp_path: Path) -> None:
    package = tmp_path / ".venv" / "Lib" / "site-packages" / "anvil" / "__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("", encoding="utf-8")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "skills").mkdir()

    assert _checkout_root(package) is None


def test_git_identity_queries_reject_unbounded_or_multiline_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    outputs = iter(["a" * 257, "abc\ndef"])

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=next(outputs))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _run_git(tmp_path, "rev-parse", "HEAD") is None
    assert _run_git(tmp_path, "rev-parse", "HEAD") is None


def test_embedded_release_identity_requires_exact_consistent_metadata(
    tmp_path: Path,
) -> None:
    identity_file = tmp_path / "_build_identity.json"
    identity_file.write_text(
        json.dumps(
            {
                "build_kind": "release_artifact",
                "display_version": "0.6.1",
                "commit": "abcdef123456",
                "tag": "v0.6.1",
                "tag_distance": 0,
                "dirty": False,
            }
        ),
        encoding="utf-8",
    )
    identity = _embedded_identity("0.6.1", identity_file)
    assert identity is not None
    assert identity.build_kind == "release_artifact"
    assert identity.display_version == "0.6.1"

    value = json.loads(identity_file.read_text(encoding="utf-8"))
    value["dirty"] = True
    identity_file.write_text(json.dumps(value), encoding="utf-8")
    assert _embedded_identity("0.6.1", identity_file) is None


def test_build_hook_requires_explicit_release_mode_for_plain_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = subprocess.run(
        ["git", "init"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    for args in (
        ("config", "user.name", "Build Identity Test"),
        ("config", "user.email", "build-identity@example.invalid"),
    ):
        result = subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    (tmp_path / "tracked.txt").write_text("identity\n", encoding="utf-8")
    for args in (("add", "."), ("commit", "-m", "identity"), ("tag", "v0.6.1")):
        result = subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    hook = runpy.run_path(str(_plugin_root() / "bin" / "hatch_build.py"))
    git_identity = hook["_git_identity"]
    source = git_identity("0.6.1", tmp_path)
    assert source["build_kind"] == "source_artifact"
    assert source["display_version"].startswith("0.6.1+0.g")

    monkeypatch.setenv("ANVIL_RELEASE_BUILD", "1")
    release = git_identity("0.6.1", tmp_path)
    assert release["build_kind"] == "release_artifact"
    assert release["display_version"] == "0.6.1"

    injected = tmp_path / "bin" / "src" / "anvil" / "injected.py"
    injected.parent.mkdir(parents=True)
    injected.write_text("# untracked package input\n", encoding="utf-8")
    dirty = git_identity("0.6.1", tmp_path)
    assert dirty["build_kind"] == "source_artifact"
    assert dirty["dirty"] is True
    assert dirty["display_version"].endswith(".dirty")
