"""Guard the one-line installer ``scripts/install.sh``.

Two cheap, network-free checks: the script is valid POSIX sh, and running it
with no harness argument prints usage and exits 2 *before* it touches uv, git,
or the network (the arg check is the first thing it does).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _script() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "install.sh"


def test_install_script_is_valid_sh() -> None:
    r = subprocess.run(
        ["sh", "-n", str(_script())], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr


def test_install_script_requires_a_harness_arg() -> None:
    # No arg → usage + exit 2, before any uv/git/network work.
    r = subprocess.run(
        ["sh", str(_script())], capture_output=True, text=True
    )
    assert r.returncode == 2
    assert "Usage" in r.stderr


def test_install_script_force_updates_cache_and_fails_loud() -> None:
    """A stale cached checkout must be FORCED to latest main, not fail-soft to old
    code: `pull --ff-only || warn` once kept running pre-fix anvil that corrupted
    configs. The updater now resets hard to a fetched ref and exits on failure."""
    text = _script().read_text()
    assert "pull --ff-only" not in text  # no more fail-soft update
    assert "reset --hard" in text and "FETCH_HEAD" in text  # force to latest main
    # Anchor on the unique cache-update error marker, then assert it EXITS nearby
    # (rather than continuing to run stale code). Robust to other elif/else blocks.
    marker = "couldn't update the cached anvil"
    assert marker in text
    assert "exit 1" in text.split(marker, 1)[1][:300]
