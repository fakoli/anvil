"""Bounded build provenance for CLI and capability manifests.

The package version is the release-line identity.  A source checkout can move
ahead of that release between publishes, so reporting only ``__version__``
makes an unreleased checkout indistinguishable from the immutable wheel.  This
module adds Git provenance when (and only when) the package is running from an
Anvil checkout.  Installed wheels never shell out to Git.
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

_DESCRIBE_RE = re.compile(
    r"^v(?P<tag>\d+\.\d+\.\d+)-(?P<distance>\d+)-g(?P<commit>[0-9a-fA-F]{7,40})"
    r"(?P<dirty>-dirty)?$"
)
_MAX_GIT_OUTPUT = 256
_MAX_IDENTITY_BYTES = 4096
_ARTIFACT_KINDS = {"release_artifact", "source_artifact", "artifact_unknown"}


@dataclass(frozen=True)
class BuildIdentity:
    """Public, JSON-safe identity for one running Anvil build."""

    build_kind: str
    display_version: str
    commit: str | None
    tag: str | None
    tag_distance: int | None
    dirty: bool

    def to_dict(self) -> dict[str, str | int | bool | None]:
        return asdict(self)


def _checkout_root(package_file: Path) -> Path | None:
    """Return the repository root for a source/editable install, if present."""

    for candidate in package_file.resolve().parents:
        source_package = (candidate / "bin" / "src" / "anvil").resolve()
        if (
            (candidate / "bin" / "pyproject.toml").is_file()
            and (candidate / "skills").is_dir()
            and package_file.resolve().is_relative_to(source_package)
        ):
            return candidate
    return None


def _run_git(root: Path, *args: str) -> str | None:
    """Run one bounded, non-interactive Git query and return a short line."""

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
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value or "\n" in value or len(value) > _MAX_GIT_OUTPUT:
        return None
    return value


def _source_identity(
    base_version: str,
    root: Path,
    *,
    run_git: Callable[..., str | None] = _run_git,
) -> BuildIdentity:
    top_level = run_git(root, "rev-parse", "--show-toplevel")
    try:
        is_checkout_root = (
            top_level is not None and Path(top_level).resolve() == root.resolve()
        )
    except OSError:
        is_checkout_root = False
    if not is_checkout_root:
        return _unknown_source_identity(base_version)

    described = run_git(
        root,
        "describe",
        "--tags",
        "--long",
        "--dirty",
        "--match",
        "v[0-9]*",
    )
    match = _DESCRIBE_RE.fullmatch(described or "")
    if match is not None:
        tag = match.group("tag")
        distance = int(match.group("distance"))
        commit = match.group("commit").lower()
        dirty = match.group("dirty") is not None
        local = f"{distance}.g{commit}"
        if dirty:
            local += ".dirty"
        return BuildIdentity(
            build_kind="source_checkout",
            display_version=f"{base_version}+{local}",
            commit=commit,
            tag=f"v{tag}",
            tag_distance=distance,
            dirty=dirty,
        )

    return _unknown_source_identity(base_version)


def _unknown_source_identity(base_version: str) -> BuildIdentity:
    """Return an explicit non-release identity when Git proof is unavailable."""
    return BuildIdentity(
        build_kind="source_unknown",
        display_version=f"{base_version}+source.unknown",
        commit=None,
        tag=None,
        tag_distance=None,
        dirty=False,
    )


def _artifact_unknown(base_version: str) -> BuildIdentity:
    """Return a visible non-release identity for absent/invalid build metadata."""

    return BuildIdentity(
        build_kind="artifact_unknown",
        display_version=f"{base_version}+artifact.unknown",
        commit=None,
        tag=None,
        tag_distance=None,
        dirty=False,
    )


def _embedded_identity(base_version: str, path: Path) -> BuildIdentity | None:
    """Load bounded build-time provenance, rejecting inconsistent release claims."""

    try:
        raw = path.read_bytes()
        if not raw or len(raw) > _MAX_IDENTITY_BYTES:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    build_kind = value.get("build_kind")
    display_version = value.get("display_version")
    commit = value.get("commit")
    tag = value.get("tag")
    tag_distance = value.get("tag_distance")
    dirty = value.get("dirty")
    if build_kind not in _ARTIFACT_KINDS:
        return None
    if not isinstance(display_version, str) or len(display_version) > 128:
        return None
    if commit is not None and not (
        isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{7,40}", commit)
    ):
        return None
    if tag is not None and not (
        isinstance(tag, str) and re.fullmatch(r"v\d+\.\d+\.\d+", tag)
    ):
        return None
    if tag_distance is not None and (
        isinstance(tag_distance, bool)
        or not isinstance(tag_distance, int)
        or tag_distance < 0
    ):
        return None
    if not isinstance(dirty, bool):
        return None
    if build_kind == "release_artifact":
        if not (
            display_version == base_version
            and commit is not None
            and tag == f"v{base_version}"
            and tag_distance == 0
            and dirty is False
        ):
            return None
    elif build_kind == "source_artifact":
        if not display_version.startswith(f"{base_version}+") or commit is None:
            return None
    elif not (
        display_version == f"{base_version}+artifact.unknown"
        and commit is None
        and tag is None
        and tag_distance is None
        and dirty is False
    ):
        return None
    return BuildIdentity(
        build_kind=build_kind,
        display_version=display_version,
        commit=commit,
        tag=tag,
        tag_distance=tag_distance,
        dirty=dirty,
    )


@functools.lru_cache(maxsize=1)
def get_build_identity() -> BuildIdentity:
    """Return the running build identity, probing Git at most once per process."""

    from anvil import __version__

    root = _checkout_root(Path(__file__))
    embedded = _embedded_identity(
        __version__, Path(__file__).with_name("_build_identity.json")
    )
    if root is None:
        return embedded or _artifact_unknown(__version__)
    source = _source_identity(__version__, root)
    if source.build_kind != "source_unknown":
        return source
    return embedded or source


__all__ = ["BuildIdentity", "get_build_identity"]
