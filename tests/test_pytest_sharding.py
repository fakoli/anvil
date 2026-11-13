"""Complete, disjoint pytest partitions with unchanged live-test authority."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PARSE_SHARD = runpy.run_path(str(ROOT / "tests/conftest.py"))["_parse_test_shard"]


@pytest.mark.parametrize("value,expected", [("1/1", (1, 1)), ("1/4", (1, 4)),
                                            ("4/4", (4, 4)), ("2/4", (2, 4))])
def test_parse_valid_shard(value, expected):
    assert PARSE_SHARD(value) == expected


@pytest.mark.parametrize("value", ["", "1", "1/2/3", "a/4", "0/4", "5/4", "1/0", "-1/4"])
def test_reject_invalid_shard(value):
    with pytest.raises(pytest.UsageError, match="anvil-test-shard"):
        PARSE_SHARD(value)


def _collect(shard=None, *, marker=None, ambient=None):
    arguments = [sys.executable, "-m", "pytest", "tests/test_pytest_sharding.py",
                 "tests/test_github_issues_live.py", "--collect-only", "-q"]
    if shard:
        arguments.extend(["--anvil-test-shard", shard])
    if marker:
        arguments.extend(["-m", marker])
    process = subprocess.run(
        arguments, cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTEST_ADDOPTS": ambient or ""},
    )
    if ambient:
        assert process.returncode != 0
        assert "explicitly" in process.stdout + process.stderr
        return set()
    assert process.returncode == 0, process.stdout + process.stderr
    return {line for line in process.stdout.splitlines()
            if line.startswith("tests/") and "::" in line}


def test_shards_cover_actual_collection_once_and_preserve_live_exclusion():
    complete = _collect()
    shards = [_collect(f"{index}/4") for index in range(1, 5)]
    assert all(shards)
    assert set.union(*shards) == complete
    assert sum(map(len, shards)) == len(complete)
    assert not any("test_github_issues_live.py" in node for node in complete)
    assert _collect("1/1", marker="live_github") == _collect(marker="live_github")


def test_inherited_sharding_is_rejected():
    _collect(ambient="--anvil-test-shard=1/4")


def test_xdist_executes_only_the_selected_partition():
    process = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_pytest_sharding.py::test_parse_valid_shard",
         "--anvil-test-shard=2/4", "-n", "2", "--dist", "worksteal", "-q"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert "1 passed" in process.stdout


def test_every_partition_gates_stable_ci_and_publication_contexts():
    workflows = {name: yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())
                 for name in ("ci", "publish", "python-tests")}
    ci = workflows["ci"]["jobs"]
    for aggregate, partition in (("test", "linux-python-tests"),
                                 ("windows-contracts", "windows-delivery-tests")):
        job = ci[aggregate]
        assert job["needs"] == partition and "always()" in job["if"]
        guard = job["steps"][0]
        assert guard["run"] == 'test "$SHARDS_RESULT" = success'
        assert guard["env"]["SHARDS_RESULT"] == "${{ needs." + partition + ".result }}"
        assert ci[partition]["uses"] == "./.github/workflows/python-tests.yml"
    assert ci["test"]["name"] == "test (python 3.11)"
    assert ci["test"]["strategy"]["matrix"] == {"python-version": ["3.11"]}
    assert ci["windows-contracts"]["name"] == "windows focused contracts"
    assert ci["windows-delivery-tests"]["with"]["windows_delivery"] is True

    reusable = workflows["python-tests"]
    assert reusable["permissions"] == {"contents": "read"}
    partition = reusable["jobs"]["partition"]
    assert partition["strategy"] == {"fail-fast": False, "matrix": {"shard": [1, 2, 3, 4]}}
    runs = [step for step in partition["steps"] if step["name"].startswith("Run ")]
    assert len(runs) == 2 and {str(step["if"]) for step in runs} == {
        "${{ !inputs.windows_delivery }}", "inputs.windows_delivery"}
    for step in runs:
        assert "--anvil-test-shard ${{ matrix.shard }}/4" in step["run"]
        assert "-n 4 --dist worksteal" in step["run"]
        assert "--durations=30" in step["run"] and "--junitxml" in step["run"]
    upload = partition["steps"][-1]
    assert upload["if"] == "always()" and upload["with"]["if-no-files-found"] == "error"
    assert "matrix.shard" in upload["with"]["name"]
    publish = workflows["publish"]["jobs"]
    assert publish["build"]["needs"] == ["source", "python-tests"]
    assert publish["python-tests"]["needs"] == "source"
    assert publish["python-tests"]["with"]["ref"] == "${{ needs.source.outputs.commit }}"
    assert publish["source"]["permissions"] == {"contents": "read"}
    assert publish["source"]["outputs"]["commit"] == "${{ steps.source.outputs.commit }}"
    assert publish["python-tests"]["uses"] == "./.github/workflows/python-tests.yml"
    assert publish["publish"]["needs"] == "build"
    assert publish["publish"]["permissions"] == {"id-token": "write"}
