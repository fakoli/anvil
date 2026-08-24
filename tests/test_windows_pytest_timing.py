from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "windows_pytest_timing.py"
SPEC = importlib.util.spec_from_file_location("windows_pytest_timing", HELPER)
assert SPEC and SPEC.loader
timing = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = timing
SPEC.loader.exec_module(timing)


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _configuration_args(repo: Path, **overrides: object) -> argparse.Namespace:
    values = {
        "repo": str(repo),
        "warmups": 1,
        "samples": 3,
        "workers": 16,
        "timeout_seconds": 120,
        "output": "artifacts/windows-pytest-timing.json",
        "expected_commit": None,
        "host_label": "test-host",
        "probe": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _qualified_artifact(
    tmp_path: Path,
) -> tuple[dict[str, Any], timing.Configuration]:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    config = timing.Configuration(
        repo=tmp_path,
        output=artifacts / "result.json",
        artifacts=artifacts,
        cache=tmp_path / "cache",
        log_root=artifacts / "logs",
        log_relative_root="artifacts/logs",
        warmups=0,
        samples=2,
        workers=2,
        timeout_seconds=10,
        expected_commit=None,
        host_label="test-host",
        probe=False,
    )
    counts = {"tests": 10, "failures": 0, "errors": 0, "skipped": 0, "passed": 10}
    runs = [
        {
            "phase": "measured",
            "mode": "parallel",
            "sample": position,
            "timing_valid": True,
            "comparison_qualified": True,
            "elapsed_seconds": elapsed,
            "junit_counts": counts,
            "junit_testcase_ids_sha256": "same-workload",
        }
        for position, elapsed in enumerate((10.0, 5.0), start=1)
    ]
    artifact: dict[str, Any] = {
        "versions": {"observable": True},
        "controls": {
            "comparison_ready": True,
            "defender_exclusions_visibility": {
                group: "unavailable" for group in timing.DEFENDER_EXCLUSION_GROUPS
            },
        },
        "collection": {
            "error": None,
            "count": 10,
            "node_ids_sha256": "collected-nodes",
            "junit_testcase_ids_sha256": "same-workload",
            "source_files_sha256": "tracked-sources",
        },
        "runs": runs,
    }
    return artifact, config


def test_clean_git_preflight_rejects_untracked_files(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    identity = timing.require_clean_git(repo)
    (repo / "untracked-test.txt").write_text("must fail\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="repository_not_fully_clean"):
        timing.require_clean_git(repo, identity)


def test_clean_git_preflight_binds_commit_tree_and_index(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    identity = timing.require_clean_git(repo)
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="repository_not_fully_clean"):
        timing.require_clean_git(repo, identity)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_clean_git_rejects_index_flags_that_hide_worktree_bytes(
    tmp_path: Path, flag: str
) -> None:
    repo = _clean_repo(tmp_path)
    identity = timing.require_clean_git(repo)
    _git(repo, "update-index", flag, "tracked.txt")
    (repo / "tracked.txt").write_text("hidden change\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="tracked_file_has_hidden_index_flag"):
        timing.require_clean_git(repo, identity)


def test_clean_git_rejects_untracked_file_hidden_by_repo_exclude(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    identity = timing.require_clean_git(repo)
    (repo / ".git" / "info" / "exclude").write_text(
        "/pytest.ini\n", encoding="utf-8"
    )
    (repo / "pytest.ini").write_text(
        "[pytest]\naddopts = --ignore=tests/test_hidden.py\n", encoding="utf-8"
    )
    assert _git(repo, "status", "--porcelain") == ""

    with pytest.raises(RuntimeError, match="repository_not_fully_clean"):
        timing.require_clean_git(repo, identity)


def test_collected_nodes_must_come_from_tracked_sources() -> None:
    nodes = [
        "tests/test_alpha.py::test_one",
        "tests/test_alpha.py::test_two[value]",
        "tests/test_beta.py::TestGroup::test_three",
    ]

    sources, digest = timing._validate_collected_nodes(
        nodes, {"tests/test_alpha.py", "tests/test_beta.py"}
    )

    assert sources == ["tests/test_alpha.py", "tests/test_beta.py"]
    assert len(digest) == 64

    with pytest.raises(RuntimeError, match="collection_contains_untracked_source"):
        timing._validate_collected_nodes(nodes, {"tests/test_alpha.py"})


def test_non_probe_protocol_is_fixed_to_one_warmup_and_three_runs(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)

    with pytest.raises(ValueError, match="fixed_at_one_warmup_and_three_runs"):
        timing._configuration(_configuration_args(repo, samples=5))


def test_non_probe_worker_count_is_fixed_at_selected_optimum(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)

    with pytest.raises(ValueError, match="workers_fixed_at_16"):
        timing._configuration(_configuration_args(repo, workers=8))


def test_output_must_be_direct_artifacts_child(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)

    with pytest.raises(ValueError, match="direct_artifacts_child"):
        timing._configuration(_configuration_args(repo, output="artifacts/nested/result.json"))


def test_reparse_component_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("creating a test symlink requires Windows Developer Mode")

    with pytest.raises(ValueError, match="reparse_point"):
        timing.validate_no_reparse_components(link / "future" / "artifact.json")


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex contract")
def test_named_mutex_rejects_concurrent_owner() -> None:
    name = f"Global\\AnvilPytestTiming-Test-{os.getpid()}"
    with timing.WindowsNamedMutex(name):
        with pytest.raises(RuntimeError, match="already_held"):
            with timing.WindowsNamedMutex(name):
                pass


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive output contract")
def test_exclusive_output_reserves_only_allowed_untracked_artifact(
    tmp_path: Path,
) -> None:
    repo = _clean_repo(tmp_path)
    artifacts = repo / "artifacts"
    artifacts.mkdir()
    output = artifacts / "result.json"
    identity = timing.require_clean_git(repo)

    with timing.ExclusiveOutput(output) as reservation:
        timing.require_clean_git(
            repo,
            identity,
            allowed_untracked=("artifacts/result.json",),
        )
        with pytest.raises(OSError):
            with timing.ExclusiveOutput(output):
                pass
        reservation.write_json({"status": "reserved"})

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "status": "reserved"
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_managed_probe_is_gated_and_job_is_empty_after_exit(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    cache = tmp_path / "cache"
    log_root.mkdir()
    cache.mkdir()
    config = timing.Configuration(
        repo=tmp_path,
        output=tmp_path / "result.json",
        artifacts=tmp_path,
        cache=cache,
        log_root=log_root,
        log_relative_root="logs",
        warmups=0,
        samples=1,
        workers=2,
        timeout_seconds=10,
        expected_commit=None,
        host_label="test-host",
        probe=True,
    )
    gate = f"Local\\AnvilPytestTiming-Probe-{os.getpid()}"
    command = timing._actual_command(config, ["python", "-c", "<fixed-probe>"], gate)

    result = timing._managed_process(
        config,
        command,
        gate=gate,
        stdout_path=log_root / "stdout.log",
        stderr_path=log_root / "stderr.log",
    )

    assert result["exit_code"] == 0
    assert result["process_error"] is None
    assert result["containment_verified"] is True


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_managed_timeout_terminates_descendants_and_verifies_empty_job(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    cache = tmp_path / "cache"
    log_root.mkdir()
    cache.mkdir()
    config = timing.Configuration(
        repo=tmp_path,
        output=tmp_path / "result.json",
        artifacts=tmp_path,
        cache=cache,
        log_root=log_root,
        log_relative_root="logs",
        warmups=0,
        samples=1,
        workers=2,
        timeout_seconds=1,
        expected_commit=None,
        host_label="test-host",
        probe=True,
    )
    gate = f"Local\\AnvilPytestTiming-Timeout-{os.getpid()}"
    child_code = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "time.sleep(30)"
    )
    command = [
        sys.executable,
        str(HELPER),
        "_gate",
        str(gate),
        sys.executable,
        "-c",
        child_code,
    ]

    result = timing._managed_process(
        config,
        command,
        gate=gate,
        stdout_path=log_root / "stdout.log",
        stderr_path=log_root / "stderr.log",
    )

    assert result["timed_out"] is True
    assert result["containment_verified"] is True
    assert result["process_error"] is None


def test_insufficient_non_probe_result_exits_nonzero() -> None:
    assert timing.result_exit_code(
        probe=False,
        measurement_valid=True,
        affected_slice_median_budget_met=False,
    ) == 1
    assert timing.result_exit_code(
        probe=False,
        measurement_valid=False,
        affected_slice_median_budget_met=True,
    ) == 1
    assert timing.result_exit_code(
        probe=False,
        measurement_valid=True,
        affected_slice_median_budget_met=True,
    ) == 0


def test_redacted_defender_exclusions_do_not_require_elevation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        timing,
        "_power_snapshot",
        lambda: {
            "observable": True,
            "active_scheme_guid": "00000000-0000-0000-0000-000000000001",
            "full_settings_sha256": "power",
            "power_source": "ac",
            "error": None,
        },
    )
    monkeypatch.setattr(
        timing,
        "_defender_snapshot",
        lambda: {
            "status": {
                "antivirus_enabled": True,
                "realtime_protection_enabled": True,
                "behavior_monitor_enabled": True,
                "ioav_protection_enabled": True,
                "tamper_protected": True,
            },
            "exclusions": {
                group: {
                    "availability": "unavailable",
                    "unavailable_reason": "permission_limited",
                    "count": None,
                    "sha256": None,
                }
                for group in timing.DEFENDER_EXCLUSION_GROUPS
            },
            "status_error": None,
            "exclusions_error": None,
            "error": None,
        },
    )

    snapshot = timing.control_snapshot()
    artifact, config = _qualified_artifact(tmp_path)
    artifact["controls"] = snapshot

    assert snapshot["comparison_ready"] is True
    assert snapshot["defender_exclusions_visibility"] == {
        group: "unavailable" for group in timing.DEFENDER_EXCLUSION_GROUPS
    }
    assert timing._finish_artifact(config, artifact) == 0
    assert artifact["result"]["status"] == "passed"
    assert artifact["result"]["affected_slice_median_budget_met"] is True


def test_partial_defender_status_is_not_comparable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        timing,
        "_power_snapshot",
        lambda: {"observable": True, "power_source": "ac", "error": None},
    )
    monkeypatch.setattr(
        timing,
        "_defender_snapshot",
        lambda: {
            "status": {"antivirus_enabled": True},
            "status_error": None,
            "exclusions": {
                group: {
                    "availability": "unavailable",
                    "unavailable_reason": "permission_limited",
                    "count": None,
                    "sha256": None,
                }
                for group in timing.DEFENDER_EXCLUSION_GROUPS
            },
            "exclusions_error": None,
            "error": None,
        },
    )

    assert timing.control_snapshot()["comparison_ready"] is False


def test_each_visible_defender_exclusion_group_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {field: True for field in timing.DEFENDER_STATUS_FIELDS}
    exclusions = {
        "paths": {
            "availability": "unavailable",
            "unavailable_reason": "permission_limited",
            "count": None,
            "sha256": None,
        },
        "processes": {
            "availability": "available",
            "unavailable_reason": None,
            "count": 1,
            "sha256": "process-a",
        },
        "extensions": {
            "availability": "available",
            "unavailable_reason": None,
            "count": 0,
            "sha256": "extensions",
        },
    }
    monkeypatch.setattr(
        timing,
        "_power_snapshot",
        lambda: {"observable": True, "power_source": "ac", "error": None},
    )
    monkeypatch.setattr(
        timing,
        "_defender_snapshot",
        lambda: {
            "status": status,
            "status_error": None,
            "exclusions": exclusions,
            "exclusions_error": None,
            "error": None,
        },
    )
    before = timing.control_snapshot()
    exclusions["processes"] = {**exclusions["processes"], "sha256": "process-b"}
    after = timing.control_snapshot()

    assert before["comparison_ready"] is True
    assert before["defender_exclusions_visibility"]["paths"] == "unavailable"
    assert before["defender_exclusions_visibility"]["processes"] == "available"
    assert (
        before["comparison_fingerprint_sha256"]
        != after["comparison_fingerprint_sha256"]
    )


def test_insufficient_artifact_keeps_descriptive_median_and_null_budget(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    artifacts = repo / "artifacts"
    artifacts.mkdir()
    config = timing.Configuration(
        repo=repo,
        output=artifacts / "result.json",
        artifacts=artifacts,
        cache=repo / ".anvil-build" / "cache",
        log_root=artifacts / "logs" / "run",
        log_relative_root="artifacts/logs/run",
        warmups=0,
        samples=2,
        workers=4,
        timeout_seconds=60,
        expected_commit=None,
        host_label="test-host",
        probe=False,
    )
    base_counts = {"tests": 10, "failures": 0, "errors": 0, "skipped": 1, "passed": 9}
    runs = [
        {
            "phase": "measured",
            "mode": "parallel",
            "sample": 1,
            "timing_valid": True,
            "comparison_qualified": False,
            "elapsed_seconds": 10.0,
            "junit_counts": base_counts,
            "junit_testcase_ids_sha256": "same-workload",
        },
        {
            "phase": "measured",
            "mode": "parallel",
            "sample": 2,
            "timing_valid": True,
            "comparison_qualified": False,
            "elapsed_seconds": 5.0,
            "junit_counts": base_counts,
            "junit_testcase_ids_sha256": "same-workload",
        },
    ]
    artifact = {
        "versions": {"observable": True},
        "controls": {
            "comparison_ready": False,
            "defender_exclusions_visibility": {
                group: "unavailable" for group in timing.DEFENDER_EXCLUSION_GROUPS
            },
        },
        "collection": {
            "error": None,
            "count": 10,
            "node_ids_sha256": "collected-nodes",
            "source_files_sha256": "tracked-sources",
        },
        "runs": runs,
    }

    assert timing._finish_artifact(config, artifact) == 1
    written = json.loads(config.output.read_text(encoding="utf-8"))
    assert written["result"]["measurement_valid"] is False
    assert written["result"]["parallel_seconds"]["median"] == 7.5
    assert written["result"]["affected_slice_median_budget_met"] is None


def test_junit_count_change_invalidates_protocol(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    config = timing.Configuration(
        repo=tmp_path,
        output=artifacts / "result.json",
        artifacts=artifacts,
        cache=tmp_path / "cache",
        log_root=artifacts / "logs",
        log_relative_root="artifacts/logs",
        warmups=0,
        samples=2,
        workers=2,
        timeout_seconds=10,
        expected_commit=None,
        host_label="test-host",
        probe=False,
    )
    runs = []
    for sample, tests in ((1, 10), (2, 9)):
        runs.append(
            {
                "phase": "measured",
                "mode": "parallel",
                "sample": sample,
                "timing_valid": True,
                "comparison_qualified": True,
                "elapsed_seconds": 1.0,
                "junit_counts": {
                    "tests": tests,
                    "failures": 0,
                    "errors": 0,
                    "skipped": 0,
                    "passed": tests,
                },
                "junit_testcase_ids_sha256": "same-workload",
            }
        )
    artifact = {
        "versions": {"observable": True},
        "controls": {
            "comparison_ready": True,
            "defender_exclusions_visibility": {
                group: "available" for group in timing.DEFENDER_EXCLUSION_GROUPS
            },
        },
        "collection": {
            "error": None,
            "count": 10,
            "node_ids_sha256": "collected-nodes",
            "source_files_sha256": "tracked-sources",
        },
        "runs": runs,
    }

    assert timing._finish_artifact(config, artifact) == 1
    assert artifact["result"]["junit_counts_identical_across_runs"] is False


def test_junit_evidence_rejects_aggregate_without_testcases(tmp_path: Path) -> None:
    junit = tmp_path / "aggregate-only.xml"
    junit.write_text(
        '<testsuites tests="10" failures="0" errors="0" skipped="0" />',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="junit_testcase_count_mismatch"):
        timing._junit_evidence(junit)


def test_junit_evidence_rejects_duplicate_testcase_identities(tmp_path: Path) -> None:
    junit = tmp_path / "duplicate-testcases.xml"
    junit.write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="0">'
        '<testcase file="tests/test_x.py" classname="TestX" name="test_same" />'
        '<testcase file="tests/test_x.py" classname="TestX" name="test_same" />'
        "</testsuite>",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="junit_testcase_identity_duplicate"):
        timing._junit_evidence(junit)


@pytest.mark.parametrize("outcome", ["failure", "error", "skipped"])
def test_junit_evidence_rejects_false_success_aggregate(
    tmp_path: Path, outcome: str
) -> None:
    junit = tmp_path / f"hidden-{outcome}.xml"
    junit.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase file="tests/test_x.py" classname="tests.test_x" name="test_x">'
        f"<{outcome} />"
        "</testcase></testsuite>",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="junit_aggregate_outcome_mismatch"):
        timing._junit_evidence(junit)


def test_junit_identity_digest_matches_collection_nodeids(tmp_path: Path) -> None:
    junit = tmp_path / "identity.xml"
    junit.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="tests.test_x.TestX" name="test_case[param]" />'
        "</testsuite>",
        encoding="utf-8",
    )

    _, junit_digest = timing._junit_evidence(junit)
    collection_digest = timing._collection_junit_testcase_ids_sha256(
        ["tests/test_x.py::TestX::test_case[param]"]
    )

    assert junit_digest == collection_digest


def test_identical_skipped_junit_counts_cannot_qualify(tmp_path: Path) -> None:
    artifact, config = _qualified_artifact(tmp_path)
    skipped_counts = {
        "tests": 10,
        "failures": 0,
        "errors": 0,
        "skipped": 10,
        "passed": 0,
    }
    for run in artifact["runs"]:
        run["junit_counts"] = skipped_counts

    assert timing._finish_artifact(config, artifact) == 1
    assert artifact["result"]["status"] == "insufficient"
    assert artifact["result"]["measurement_valid"] is False
    assert artifact["result"]["junit_counts_identical_across_runs"] is True
    assert artifact["result"]["junit_counts_pass"] is False
    assert artifact["result"]["affected_slice_median_budget_met"] is None


def test_invalid_collection_cannot_qualify_budget(tmp_path: Path) -> None:
    artifact, config = _qualified_artifact(tmp_path)
    artifact["collection"]["error"] = "CollectionNonzero"

    assert timing._finish_artifact(config, artifact) == 1
    assert artifact["result"]["collection_valid"] is False
    assert artifact["result"]["affected_slice_median_budget_met"] is None


def test_equal_counts_with_different_testcase_ids_cannot_qualify(
    tmp_path: Path,
) -> None:
    artifact, config = _qualified_artifact(tmp_path)
    artifact["runs"][1]["junit_testcase_ids_sha256"] = "different-workload"

    assert timing._finish_artifact(config, artifact) == 1
    assert artifact["result"]["junit_testcase_ids_identical_across_runs"] is False
    assert artifact["result"]["affected_slice_median_budget_met"] is None


def test_consistently_different_junit_identities_cannot_qualify(
    tmp_path: Path,
) -> None:
    artifact, config = _qualified_artifact(tmp_path)
    for run in artifact["runs"]:
        run["junit_testcase_ids_sha256"] = "different-workload"

    assert timing._finish_artifact(config, artifact) == 1
    assert artifact["result"]["junit_testcase_ids_identical_across_runs"] is True
    assert artifact["result"]["junit_identities_match_collection"] is False
    assert artifact["result"]["affected_slice_median_budget_met"] is None


def test_valid_measurement_above_slice_budget_exits_nonzero(tmp_path: Path) -> None:
    artifact, config = _qualified_artifact(tmp_path)
    for run in artifact["runs"]:
        run["elapsed_seconds"] = 36.0

    assert timing._finish_artifact(config, artifact) == 1
    assert artifact["result"]["measurement_valid"] is True
    assert artifact["result"]["affected_slice_median_budget_met"] is False
    assert artifact["result"]["status"] == "regression"


def test_powershell_entrypoint_keeps_locked_exact_public_protocol() -> None:
    script = (ROOT / "scripts" / "measure-windows-pytest.ps1").read_text(encoding="utf-8")
    assert '[int]$Warmups = 1' in script
    assert '[int]$Samples = 3' in script
    assert '[int]$Workers = 16' in script
    assert '[int]$TimeoutSeconds = 120' in script
    assert "$Modes" not in script
    assert '"run", "--locked", "--exact", "--project"' in script


def test_timing_workload_is_only_the_git_fixture_contract() -> None:
    command = timing._public_command(16, "artifacts/logs/result.xml", probe=False)

    assert timing.TIMING_TEST_TARGETS == (
        "tests/test_git_ops.py",
        "tests/test_reconciliation.py",
    )
    assert "tests" not in command
    assert command[command.index("-n") + 1] == "16"
    assert "0" not in command
    assert command[command.index("pytest") + 1 : command.index("-q")] == list(
        timing.TIMING_TEST_TARGETS
    )


def test_timing_collection_contract_rejects_count_source_or_identity_drift() -> None:
    nodes = [f"tests/test_git_ops.py::test_{number}" for number in range(167)]
    sources = sorted(timing.TIMING_TEST_TARGETS)

    with pytest.raises(RuntimeError, match="timing_collection_contract_mismatch"):
        timing._validate_timing_collection(nodes[:-1], sources)
    with pytest.raises(RuntimeError, match="timing_collection_contract_mismatch"):
        timing._validate_timing_collection(nodes, ["tests/test_git_ops.py"])
    with pytest.raises(RuntimeError, match="timing_collection_contract_mismatch"):
        timing._validate_timing_collection(nodes, sources)


def test_windows_ci_runs_timing_harness_mechanics_separately() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "tests/test_cli.py::TestSampleSourceBindingContract" in workflow
    assert "tests/test_windows_pytest_timing.py" in workflow
    assert "anvil-source-binding" in workflow
    assert "anvil-timing-harness" in workflow
    assert "anvil-git-fixture-contract" in workflow
    assert "tests/test_git_ops.py" in workflow
    assert "tests/test_reconciliation.py" in workflow
    assert "-n 16" in workflow
    assert "uv run --project bin pytest -n auto" in workflow
