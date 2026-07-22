"""Regression coverage for composable slow/live marker selection."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_PARTITION_TARGETS = (
    "tests/test_git_ops.py",
    "tests/test_reconciliation.py",
    "tests/test_github_issues_live.py",
    "tests/test_cli.py::TestClaimBranchOption",
    "tests/test_cli.py::TestE2EClaimRelease",
    "tests/test_cli.py::TestMergeCheck",
    "tests/test_cli.py::TestApplyMergeCheck",
)


def _collected_nodes(
    marker_expression: str | tuple[str, ...] | None = None,
    *,
    allow_empty: bool = False,
    pytest_addopts: str | None = None,
) -> set[str]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        *_PARTITION_TARGETS,
        "--collect-only",
        "-q",
    ]
    if isinstance(marker_expression, str):
        expressions = (marker_expression,)
    else:
        expressions = marker_expression or ()
    for expression in expressions:
        command.extend(("-m", expression))
    env = {**os.environ, "PYTEST_ADDOPTS": pytest_addopts or ""}
    result = subprocess.run(
        command,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )
    expected_exit_codes = {0, 5} if allow_empty else {0}
    assert result.returncode in expected_exit_codes, result.stdout + result.stderr
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
    mixed_live = _collected_nodes("not live_github or not slow")
    broad_live = _collected_nodes("live_github or slow or not slow")
    parenthesized_live = _collected_nodes("(live_github)", allow_empty=True)
    live_then_fast = _collected_nodes(("live_github", "not slow"))
    fast_then_live = _collected_nodes(("not slow", "live_github"))

    assert bare_fast == safe_fast
    assert default == slow | safe_fast
    assert slow.isdisjoint(safe_fast)
    assert default.isdisjoint(live)
    assert mixed_live == default
    assert broad_live == default
    assert parenthesized_live == set()
    assert live_then_fast == safe_fast
    assert fast_then_live == live
    assert default | live == slow | safe_fast | live
    assert len(slow) == 70
    assert len(safe_fast) == 59
    assert len(live) == 3


def test_ambient_live_marker_is_not_external_write_authorization() -> None:
    """Only an explicit CLI marker may enable credentialed GitHub tests."""
    ambient_only = _collected_nodes(
        allow_empty=True,
        pytest_addopts="-m live_github",
    )
    explicit = _collected_nodes(
        "live_github",
        pytest_addopts='-m "not slow"',
    )

    assert ambient_only == set()
    assert len(explicit) == 3


def test_malformed_live_expression_refuses_before_collection() -> None:
    """Invalid marker syntax cannot become an accidental live-test opt-in."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *_PARTITION_TARGETS,
            "--collect-only",
            "-q",
            "-m",
            "live_github and (",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
    )

    assert result.returncode != 0
    assert "Wrong expression passed to '-m'" in result.stdout + result.stderr
