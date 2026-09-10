"""Pytest wrapper for the anvil-pi extension node test driver.

The actual suite is tests/anvil_pi/extension.test.mjs (node + jiti from the pi
harness install, fake `anvil` on PATH, hermetic). This wrapper keeps the repo's
pytest entry point green and skips when node is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER = REPO_ROOT / "tests" / "anvil_pi" / "extension.test.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("sh") is None,
    reason="node + POSIX sh are required for the anvil-pi extension tests",
)


def test_anvil_pi_extension_suite() -> None:
    assert DRIVER.exists(), f"missing driver: {DRIVER}"
    r = subprocess.run(
        ["node", str(DRIVER)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO_ROOT),
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "tests passed" in r.stdout