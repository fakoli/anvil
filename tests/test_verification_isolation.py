"""Repository checks cannot inherit the operator's native owner state."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from anvil.config import _home_dir
from anvil.roots.registry import RootSetRegistry


def test_default_home_is_empty_and_per_test(tmp_path: Path) -> None:
    assert Path.home().parent == tmp_path.parent
    assert Path.home() != tmp_path
    assert not list(tmp_path.iterdir())
    assert _home_dir() == Path.home()
    registry = RootSetRegistry()
    assert registry.base == Path.home() / ".anvil" / "root-sets"
    assert not registry.base.exists()
    assert os.environ["XDG_CONFIG_HOME"] == str(Path.home() / ".config")
    assert all(key not in os.environ for key in ("ANVIL_ROOT", "ANVIL_PRD", "ANVIL_ACTOR", "ANVIL_CLAIM_ID"))


def test_explicit_environment_override_keeps_platform_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = tmp_path / "explicit-home"
    monkeypatch.setenv("HOME", str(explicit))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(explicit))
    assert Path.home() == explicit
    assert _home_dir() == explicit


def test_explicit_path_home_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = tmp_path / "explicit-path-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: explicit))
    assert Path.home() == explicit
    assert RootSetRegistry().base == explicit / ".anvil" / "root-sets"


def test_subprocess_inherits_disposable_owner_home(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; print(Path.home())"],
        capture_output=True, text=True, check=True,
    )
    assert Path(result.stdout.strip()) == Path.home()


@pytest.mark.parametrize("explicit_cache", [None, "absolute", "relative"])
def test_fixture_preserves_resolved_uv_cache_and_restores_environment(
    tmp_path: Path, explicit_cache: str | None,
) -> None:
    from types import SimpleNamespace

    from tests.conftest import isolated_native_home, uv_cache_dir

    owner = tmp_path / "owner"
    owner.mkdir()
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    with pytest.MonkeyPatch.context() as baseline:
        baseline.setenv("HOME", str(owner))
        if os.name == "nt":
            baseline.setenv("USERPROFILE", str(owner))
            baseline.setenv("LOCALAPPDATA", str(owner / "local"))
        for key in ("UV_CACHE_DIR", "XDG_CACHE_HOME"):
            baseline.delenv(key, raising=False)
        if explicit_cache:
            baseline.setenv(
                "UV_CACHE_DIR",
                str(tmp_path / "cache") if explicit_cache == "absolute" else "cache",
            )
        before = subprocess.check_output(["uv", "cache", "dir"], text=True).strip()
        resolved = uv_cache_dir.__wrapped__()
        previous = dict(os.environ)
        with pytest.MonkeyPatch.context() as scoped:
            isolated_native_home.__wrapped__(
                SimpleNamespace(mktemp=lambda _: isolated), scoped, resolved,
            )
            after = subprocess.check_output(["uv", "cache", "dir"], text=True).strip()
            assert after == before
            assert Path.home() == isolated
            assert not (isolated / ".anvil").exists()
        assert dict(os.environ) == previous
