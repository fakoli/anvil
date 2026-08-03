#!/usr/bin/env python3
"""Enforce the frozen skill/CLI contract for the candidate package version."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

if __package__:
    from scripts.check_skill_cli_contract import (
        ContractError,
        contract_digest,
        load_manifest,
    )
else:
    from check_skill_cli_contract import ContractError, contract_digest, load_manifest


def _read_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must contain a JSON object")
    return value


def _package_version(repo_root: Path) -> str:
    with (repo_root / "bin" / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    if not isinstance(version, str) or not version:
        raise ContractError("project.version must be a non-empty string")
    return version


def _tagged_snapshot(repo_root: Path, version: str) -> Mapping[str, Any] | None:
    tag = f"v{version}"
    exists = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
    )
    if exists.returncode != 0:
        return None
    relative = f"release-contracts/{version}.json"
    shown = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{tag}:{relative}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
    )
    if shown.returncode != 0:
        raise ContractError(f"{tag} does not contain {relative}")
    value = json.loads(shown.stdout)
    if not isinstance(value, Mapping):
        raise ContractError(f"{tag}:{relative} is not a JSON object")
    return value


def verify_release_contract(
    repo_root: Path, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    version = _package_version(repo_root)
    contract = load_manifest(manifest)
    if contract["engine_version"] != version:
        raise ContractError(
            f"artifact engine {contract['engine_version']!r} does not match "
            f"project version {version!r}"
        )
    snapshot_path = repo_root / "release-contracts" / f"{version}.json"
    if not snapshot_path.is_file():
        raise ContractError(f"missing frozen release contract: {snapshot_path}")
    snapshot = _read_json(snapshot_path)
    expected = {
        "engine_version": version,
        "api_version": contract["api_version"],
        "command_count": len(contract["commands"]),
        "contract_count": len(contract["nodes"]),
        "sha256": contract_digest(manifest),
    }
    if dict(snapshot) != expected:
        raise ContractError(
            "frozen release contract does not match the installed artifact; "
            "bump the version and create a new snapshot"
        )
    tagged = _tagged_snapshot(repo_root, version)
    if tagged is not None and dict(tagged) != dict(snapshot):
        raise ContractError(
            f"release contract for v{version} differs from the immutable tag; "
            "a version bump is required"
        )
    return expected


def verify_release_artifact_identity(
    manifest: Mapping[str, Any], *, expected_tag: str, expected_commit: str
) -> None:
    """Require provenance produced only by the exact clean release build."""

    data = manifest.get("data")
    if not isinstance(data, Mapping):
        raise ContractError("manifest data must be an object")
    version = data.get("engine_version")
    expected = {
        "build_kind": "release_artifact",
        "display_version": version,
        "commit": expected_commit.lower(),
        "tag": expected_tag,
        "tag_distance": 0,
        "dirty": False,
    }
    actual = {key: data.get(key) for key in expected}
    if actual != expected:
        raise ContractError(
            "artifact provenance does not match the exact clean release tag: "
            f"expected {expected!r}, got {actual!r}"
        )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-release-tag")
    parser.add_argument("--expected-commit")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        manifest = _read_json(args.manifest)
        result = verify_release_contract(args.repo_root.resolve(), manifest)
        if bool(args.expected_release_tag) != bool(args.expected_commit):
            raise ContractError(
                "--expected-release-tag and --expected-commit must be used together"
            )
        if args.expected_release_tag is not None:
            verify_release_artifact_identity(
                manifest,
                expected_tag=args.expected_release_tag,
                expected_commit=args.expected_commit,
            )
    except (
        ContractError,
        OSError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"release contract: ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        "release contract: PASS "
        f"({result['engine_version']}, {result['command_count']} commands, "
        f"sha256={result['sha256'][:12]}...)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
