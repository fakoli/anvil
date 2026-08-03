"""Hatch build hook that embeds bounded source provenance in distributions."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ModuleNotFoundError:  # pragma: no cover - direct helper tests, not builds
    class BuildHookInterface:  # type: ignore[no-redef]
        """Minimal import fallback; real builds provide Hatchling."""

_DESCRIBE_RE = re.compile(
    r"^v(?P<tag>\d+\.\d+\.\d+)-(?P<distance>\d+)-g"
    r"(?P<commit>[0-9a-fA-F]{7,40})(?P<dirty>-dirty)?$"
)
_MAX_OUTPUT = 256
_MAX_EMBEDDED = 4096


def _run_git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "-c", "core.pager=cat", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or not value or "\n" in value or len(value) > _MAX_OUTPUT:
        return None
    return value


def _package_tree_dirty(root: Path) -> bool | None:
    """Return whether package/build inputs differ, including untracked files."""

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "core.pager=cat",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "bin",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # A large result is still conclusively dirty; do not retain or expose it.
    return bool(result.stdout)


def _git_identity(version: str, root: Path) -> dict[str, Any] | None:
    top_level = _run_git(root, "rev-parse", "--show-toplevel")
    try:
        if top_level is None or Path(top_level).resolve() != root.resolve():
            return None
    except OSError:
        return None
    described = _run_git(
        root,
        "describe",
        "--tags",
        "--long",
        "--dirty",
        "--match",
        "v[0-9]*",
    )
    match = _DESCRIBE_RE.fullmatch(described or "")
    if match is None:
        return None
    tag = match.group("tag")
    distance = int(match.group("distance"))
    commit = _run_git(root, "rev-parse", "--short=12", "HEAD")
    if commit is None or not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
        return None
    commit = commit.lower()
    package_dirty = _package_tree_dirty(root)
    dirty = match.group("dirty") is not None or package_dirty is True
    exact_release = (
        os.environ.get("ANVIL_RELEASE_BUILD") == "1"
        and tag == version
        and distance == 0
        and not dirty
        and package_dirty is False
    )
    if exact_release:
        return {
            "build_kind": "release_artifact",
            "display_version": version,
            "commit": commit,
            "tag": f"v{tag}",
            "tag_distance": 0,
            "dirty": False,
        }
    local = f"{distance}.g{commit}" + (".dirty" if dirty else "")
    return {
        "build_kind": "source_artifact",
        "display_version": f"{version}+{local}",
        "commit": commit,
        "tag": f"v{tag}",
        "tag_distance": distance,
        "dirty": dirty,
    }


def _existing_identity(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > _MAX_EMBEDDED:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _unknown_identity(version: str) -> dict[str, Any]:
    return {
        "build_kind": "artifact_unknown",
        "display_version": f"{version}+artifact.unknown",
        "commit": None,
        "tag": None,
        "tag_distance": None,
        "dirty": False,
    }


def _project_version(project_root: Path) -> str:
    with (project_root / "pyproject.toml").open("rb") as handle:
        value = tomllib.load(handle)["project"]["version"]
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("project.version must be a release SemVer")
    return value


class CustomBuildHook(BuildHookInterface):
    """Inject provenance without writing generated files into the checkout."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        project_root = Path(self.root).resolve()
        checkout_root = project_root.parent
        package_version = _project_version(project_root)
        identity = _git_identity(package_version, checkout_root)
        if identity is None:
            identity = _existing_identity(
                project_root / "src" / "anvil" / "_build_identity.json"
            )
        if identity is None:
            identity = _unknown_identity(package_version)

        descriptor, temporary = tempfile.mkstemp(
            prefix="anvil-build-identity-", suffix=".json"
        )
        os.close(descriptor)
        temporary_path = Path(temporary)
        temporary_path.write_text(
            json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        self._temporary_path = temporary_path
        destination = (
            "anvil/_build_identity.json"
            if self.target_name == "wheel"
            else "src/anvil/_build_identity.json"
        )
        build_data.setdefault("force_include", {})[str(temporary_path)] = destination

    def finalize(
        self, version: str, build_data: dict[str, Any], artifact_path: str
    ) -> None:
        temporary = getattr(self, "_temporary_path", None)
        if isinstance(temporary, Path):
            temporary.unlink(missing_ok=True)
