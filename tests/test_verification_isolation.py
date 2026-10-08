"""Repository checks cannot inherit the operator's native owner state."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from anvil.config import _home_dir
from anvil.roots.registry import RootSetRegistry


def test_default_home_is_empty_and_per_test(tmp_path: Path) -> None:
    assert Path.home() == tmp_path / "anvil-home"
    assert _home_dir() == Path.home()
    registry = RootSetRegistry()
    assert registry.base == Path.home() / ".anvil" / "root-sets"
    assert not registry.base.exists()


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


def test_default_environment_values_are_preserved(tmp_path: Path) -> None:
    # A disposable resolver is enough; the process home and caches stay intact.
    assert Path.home() == tmp_path / "anvil-home"
    assert os.environ.get("HOME") != str(Path.home())
