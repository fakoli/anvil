"""Regression coverage for composable slow/live marker selection."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_PARTITION_TARGETS = (
    "tests/test_git_ops.py",
    "tests/test_reconciliation.py",
    "tests/test_github_issues_live.py",
)


def _collected_nodes(marker_expression: str | None = None) -> set[str]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        *_PARTITION_TARGETS,
        "--collect-only",
        "-q",
    ]
    if marker_expression is not None:
        command.extend(("-m", marker_expression))
    result = subprocess.run(
        command,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return {
        line
        for line in result.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    }


def test_fast_selection_preserves_default_live_exclusion_and_partition() -> None:
    """Default = slow + fast; credentialed live tests remain a separate set."""
    default = _collected_nodes()
    slow = _collected_nodes("slow and not live_github")
    safe_fast = _collected_nodes("not slow and not live_github")
    bare_fast = _collected_nodes("not slow")
    live = _collected_nodes("live_github")
    all_nodes = _collected_nodes("live_github or slow or not slow")

    assert bare_fast == safe_fast
    assert default == slow | safe_fast
    assert slow.isdisjoint(safe_fast)
    assert default.isdisjoint(live)
    assert all_nodes == default | live
    assert len(slow) == 50
    assert len(safe_fast) == 58
    assert len(live) == 3
