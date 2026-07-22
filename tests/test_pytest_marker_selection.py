"""Regression coverage for composable slow/live marker selection."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LIVE_SELECTOR = runpy.run_path(str(_REPO_ROOT / "tests" / "conftest.py"))[
    "_explicitly_selects_live_github_args"
]
_PARTITION_TARGETS = (
    "tests/test_git_ops.py",
    "tests/test_reconciliation.py",
    "tests/test_github_issues_live.py",
    "tests/test_cli.py::TestClaimBranchOption",
    "tests/test_cli.py::TestE2EClaimRelease",
    "tests/test_cli.py::TestMergeCheck",
    "tests/test_cli.py::TestApplyMergeCheck",
    "tests/test_cli.py::TestHeartbeatLeaseWarning",
    "tests/test_cli.py::TestStatusClaimReadback",
    "tests/test_cli.py::TestClaimCommand::test_claim_happy_path_creates_lease_and_branch",
    "tests/test_doctor_preflight.py::TestPreflightTreeState",
    "tests/test_bundle_execution.py::test_bundle_claim_packet_and_progress_cli_share_coordinator_flow",
    "tests/test_cli_drift.py::TestOrphanBranchDrift",
    "tests/test_cli_drift.py::TestOrphanWorktreeFileLabel",
    "tests/test_migrate_workspace.py::test_finds_worktree_stranded_state",
    "tests/test_state_workspace.py::test_worktrees_share_one_home_workspace",
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
    assert len(slow) == 88
    assert len(safe_fast) == 61
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


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("-m", "live_github"), True),
        (("-mlive_github",), True),
        (("-m=live_github",), True),
        (("-qm", "live_github"), True),
        (("-qmlive_github",), True),
        (("-m", "not live_github", "-mlive_github"), True),
        (("-m", "live_github", "-mnot_live"), False),
        (("--", "-mlive_github"), False),
        (("-m", "not live_github", "--", "-mlive_github"), False),
        (("-m", "live_github", "--", "-mnot_live"), True),
        (("--markexpr=live_github",), False),
    ],
)
def test_live_opt_in_parser_matches_supported_pytest_argv(
    args: tuple[str, ...], expected: bool
) -> None:
    assert _LIVE_SELECTOR(args) is expected


def test_live_module_refuses_writes_when_collection_hooks_are_disabled() -> None:
    env = {
        **os.environ,
        "PYTEST_ADDOPTS": "-m live_github",
        "GITHUB_TOKEN": "not-a-real-token",
        "ANVIL_TEST_REPO": "example/scratch",
    }
    env.pop("ANVIL_RUN_LIVE_GITHUB", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--noconftest",
            "tests/test_github_issues_live.py::test_rate_limit_handling",
            "-q",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )

    assert result.returncode != 0
    assert "set ANVIL_RUN_LIVE_GITHUB=1" in result.stdout + result.stderr


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
