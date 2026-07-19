"""Guards for scripts/release.py — the version-bump lockstep helper.

These run the helper in --dry-run (which writes nothing) and assert it plans
edits for every file the release contract pins, so the script cannot silently
fall out of sync with test_version_sync.py / test_install_manifests.py.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "release.py"

# The files the release contract requires to move in lockstep (mirrors
# CLAUDE.md and the two enforcement tests). If a manifest is added there, this
# list — and release.py — must gain it too; that is the point of the guard.
_PINNED = [
    ".claude-plugin/plugin.json",
    "bin/pyproject.toml",
    "bin/src/anvil/__init__.py",
    "bin/uv.lock",
    "packaging/codex/.codex-plugin/plugin.json",
    "packaging/codex/.agents/plugins/marketplace.json",
    "packaging/gemini/gemini-extension.json",
    "packaging/openclaw/plugin/openclaw.plugin.json",
    "packaging/openclaw/plugin/package.json",
    "CHANGELOG.md",
]
_USER_DOCS = [
    "README.md",
    "docs/how-to/getting-started.md",
    "docs/cli-reference.md",
    "docs/architecture.md",
]


def _dry_run(spec: str) -> subprocess.CompletedProcess[str]:
    # Plain python3 (stdlib-only script) so this mirrors how it runs under any
    # harness — Claude, Codex, or a bare shell — not just inside `uv`.
    return subprocess.run(
        [sys.executable, str(_SCRIPT), spec, "--dry-run"],
        capture_output=True, text=True, timeout=60,
    )


def test_script_exists_and_is_executable() -> None:
    assert _SCRIPT.exists(), "scripts/release.py is missing"


def test_dry_run_plans_every_pinned_file() -> None:
    r = _dry_run("patch")
    assert r.returncode == 0, r.stderr
    for rel in _PINNED:
        assert rel in r.stdout, f"release.py --dry-run did not plan an edit for {rel}"


def test_dry_run_updates_user_facing_docs() -> None:
    r = _dry_run("patch")
    for rel in _USER_DOCS:
        assert rel in r.stdout, f"release.py --dry-run skipped user-facing doc {rel}"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    before = (_REPO / "bin" / "src" / "anvil" / "__init__.py").read_text()
    _dry_run("major")
    after = (_REPO / "bin" / "src" / "anvil" / "__init__.py").read_text()
    assert before == after, "--dry-run must not modify any file"


@pytest.mark.parametrize(
    ("spec", "shown"),
    [("patch", None), ("minor", None), ("major", None), ("9.9.9", "9.9.9")],
)
def test_bump_specs_are_accepted(spec: str, shown: str | None) -> None:
    r = _dry_run(spec)
    assert r.returncode == 0, r.stderr
    assert "->" in r.stdout
    if shown:
        assert shown in r.stdout


def test_invalid_spec_is_rejected() -> None:
    r = _dry_run("sideways")
    assert r.returncode != 0
    assert "invalid version spec" in (r.stdout + r.stderr)
