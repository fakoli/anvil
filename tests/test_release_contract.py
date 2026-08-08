"""Frozen release contracts cannot drift under an existing package version."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from anvil.cli.describe import API_VERSION
from scripts import check_release_contract
from scripts.check_skill_cli_contract import ContractError, contract_digest


def _manifest(version: str = "1.2.3") -> dict[str, Any]:
    return {
        "ok": True,
        "command": "describe",
        "data": {
            "api_version": "9",
            "engine_version": version,
            "cli": {
                "commands": ["scan"],
                "options": {"scan": []},
                "contracts": [
                    {"path": [], "kind": "group", "flags": ["--version"]},
                    {"path": ["scan"], "kind": "command", "flags": []},
                ],
                "contract_count": 2,
                "count": 1,
            },
        },
    }


def _repo(tmp_path: Path, manifest: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "pyproject.toml").write_text(
        '[project]\nname = "anvil-state"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    expected = {
        "engine_version": "1.2.3",
        "api_version": "9",
        "command_count": 1,
        "contract_count": 2,
        "sha256": contract_digest(manifest),
    }
    contracts = tmp_path / "release-contracts"
    contracts.mkdir()
    (contracts / "1.2.3.json").write_text(
        json.dumps(expected), encoding="utf-8"
    )
    return tmp_path, expected


def test_matching_untagged_candidate_contract_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    repo, expected = _repo(tmp_path, manifest)
    monkeypatch.setattr(check_release_contract, "_tagged_snapshot", lambda *_: None)

    assert check_release_contract.verify_release_contract(repo, manifest) == expected


def test_artifact_drift_requires_version_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    repo, _ = _repo(tmp_path, manifest)
    manifest["data"]["cli"]["contracts"][1]["flags"] = ["--json"]
    manifest["data"]["cli"]["options"]["scan"] = ["--json"]
    monkeypatch.setattr(check_release_contract, "_tagged_snapshot", lambda *_: None)

    with pytest.raises(ContractError, match="bump the version"):
        check_release_contract.verify_release_contract(repo, manifest)


def test_snapshot_cannot_be_rewritten_after_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    repo, expected = _repo(tmp_path, manifest)
    tagged = {**expected, "sha256": "0" * 64}
    monkeypatch.setattr(
        check_release_contract, "_tagged_snapshot", lambda *_: tagged
    )

    with pytest.raises(ContractError, match="immutable tag"):
        check_release_contract.verify_release_contract(repo, manifest)


def test_real_git_tag_snapshot_cannot_be_rewritten(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    repo, _ = _repo(tmp_path, manifest)

    def git(*args: str) -> None:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    git("init")
    git("config", "user.name", "Release Contract Test")
    git("config", "user.email", "release-contract@example.invalid")
    git("add", ".")
    git("commit", "-m", "freeze contract")
    git("tag", "v1.2.3")

    manifest["data"]["cli"]["contracts"][1]["flags"] = ["--json"]
    manifest["data"]["cli"]["options"]["scan"] = ["--json"]
    changed = {
        "engine_version": "1.2.3",
        "api_version": "9",
        "command_count": 1,
        "contract_count": 2,
        "sha256": contract_digest(manifest),
    }
    (repo / "release-contracts" / "1.2.3.json").write_text(
        json.dumps(changed), encoding="utf-8"
    )

    with pytest.raises(ContractError, match="immutable tag"):
        check_release_contract.verify_release_contract(repo, manifest)


def test_exact_release_artifact_identity_is_required() -> None:
    manifest = _manifest()
    manifest["data"].update(
        {
            "display_version": "1.2.3",
            "build_kind": "release_artifact",
            "commit": "abcdef123456",
            "tag": "v1.2.3",
            "tag_distance": 0,
            "dirty": False,
        }
    )
    check_release_contract.verify_release_artifact_identity(
        manifest, expected_tag="v1.2.3", expected_commit="abcdef123456"
    )

    manifest["data"]["build_kind"] = "source_artifact"
    with pytest.raises(ContractError, match="exact clean release tag"):
        check_release_contract.verify_release_artifact_identity(
            manifest, expected_tag="v1.2.3", expected_commit="abcdef123456"
        )


def test_publish_oidc_job_is_minimal_and_all_actions_are_immutable() -> None:
    repo = Path(__file__).resolve().parents[1]
    workflow_path = repo / ".github" / "workflows" / "publish.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    build = workflow["jobs"]["build"]
    publish = workflow["jobs"]["publish"]

    assert "id-token" not in build["permissions"]
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["needs"] == "build"
    for job in (build, publish):
        for step in job["steps"]:
            action = step.get("uses")
            if action is not None:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), action

    text = workflow_path.read_text(encoding="utf-8")
    assert 'ANVIL_RELEASE_BUILD: "1"' in text
    assert "uv build \"$sdist\" --wheel" in text
    assert "--expected-release-tag" in text
    assert "prd source-name --prd CON --json" in text
    assert text.count("status --porcelain --untracked-files=all") >= 2
    assert "run_benchmark.py --scenarios overlapping_files --quick" in text
    assert '--out "$RUNNER_TEMP/anvil-benchmark-results.md"' in text
    assert "mkdocs build --strict" in text
    assert "(cd bin && uv run --locked ruff check ..)" in text
    assert f"--expected-api-version {API_VERSION}" in text
    assert (
        "printf '%s\\0' \"$wheel\" \"$sdist\" | sort -z | "
        "xargs -0 sha256sum > SHA256SUMS"
    ) in text
    assert "find bin/dist -maxdepth 1 -type f -print0" not in text
    assert "bin/dist/*.whl" in text
    assert "bin/dist/*.tar.gz" in text


def test_ci_release_contract_jobs_fetch_tags() -> None:
    repo = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load(
        (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    for job_name in ("dependency-floor", "test"):
        checkout = workflow["jobs"][job_name]["steps"][0]
        assert checkout["with"]["fetch-depth"] == 0
