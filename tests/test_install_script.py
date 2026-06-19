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
