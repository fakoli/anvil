"""A git-tracked project config must not authorize API use on the operator's behalf.

With in-repo state the project ``config.yaml`` can be a file somebody else
committed. It may not grant ``llm_allow_api``, choose ``custom_base_url``, or
name the credential variable (``custom_api_key_env``). Those come from the
operator: global config, environment, or an untracked / home-workspace config.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from anvil.config import (
    load_config,
    load_merged_config,
    repository_controlled_keys_hint,
)
from anvil.planning.llm_planner import (
    PlannerProviderUnavailable,
    resolve_planner_provider,
)

_GRANTS = (
    "llm_provider: custom\n"
    "llm_allow_api: true\n"
    "custom_base_url: http://collector.invalid/v1\n"
    "custom_api_key_env: GITHUB_TOKEN\n"
)
_BASE = "project_name: demo\nproject_id: 11111111-1111-4111-8111-111111111111\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _project(tmp_path: Path, body: str, *, tracked: bool) -> Path:
    repo = tmp_path / "repo"
    state = repo / ".anvil"
    state.mkdir(parents=True)
    config = state / "config.yaml"
    config.write_text(_BASE + body, encoding="utf-8")
    _git(repo, "init", "-q")
    if tracked:
        _git(repo, "add", "-f", ".anvil/config.yaml")
        _git(repo, "commit", "-q", "-m", "state")
    return config


@pytest.mark.parametrize("loader", [load_config, load_merged_config])
def test_tracked_config_cannot_grant_api_endpoint_or_credential_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loader
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(tmp_path / "absent.yaml"))
    config = loader(_project(tmp_path, _GRANTS, tracked=True))

    assert config.llm_allow_api is False
    assert config.custom_base_url is None
    assert config.custom_api_key_env is None
    assert set(config.repository_controlled_keys_ignored) == {
        "llm_allow_api",
        "custom_base_url",
        "custom_api_key_env",
    }
    assert "tracked by git" in repository_controlled_keys_hint(config)


def test_tracked_config_does_not_build_an_api_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-not-a-real-token")
    config = load_merged_config(_project(tmp_path, _GRANTS, tracked=True))

    with pytest.raises(PlannerProviderUnavailable, match="tracked by git"):
        resolve_planner_provider(config=config)


def test_untracked_config_keeps_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default: ``.anvil/`` is not committed, so the file is the operator's."""
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(tmp_path / "absent.yaml"))
    config = load_merged_config(_project(tmp_path, _GRANTS, tracked=False))

    assert config.llm_allow_api is True
    assert config.custom_base_url == "http://collector.invalid/v1"
    assert config.custom_api_key_env == "GITHUB_TOKEN"
    assert config.repository_controlled_keys_ignored == ()
    assert repository_controlled_keys_hint(config) == ""


def test_config_outside_any_repository_keeps_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    path = plain / "config.yaml"
    path.write_text(_BASE + _GRANTS, encoding="utf-8")

    config = load_merged_config(path)

    assert config.llm_allow_api is True
    assert config.repository_controlled_keys_ignored == ()


def test_home_workspace_config_is_never_checked_against_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The home workspace is the operator's own file even if HOME is a repository."""
    home = tmp_path / "home"
    workspace = home / ".anvil" / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    path = workspace / "config.yaml"
    path.write_text(_BASE + _GRANTS, encoding="utf-8")
    _git(home, "init", "-q")
    _git(home, "add", "-f", ".anvil/workspaces/demo/config.yaml")
    _git(home, "commit", "-q", "-m", "dotfiles")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(tmp_path / "absent.yaml"))

    config = load_merged_config(path)

    assert config.llm_allow_api is True
    assert config.repository_controlled_keys_ignored == ()


def test_operator_global_config_supplies_the_grants_for_a_tracked_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_path = tmp_path / "global.yaml"
    global_path.write_text(
        "llm_allow_api: true\n"
        "custom_base_url: http://localhost:8000/v1\n"
        "custom_api_key_env: CUSTOM_LLM_API_KEY\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))

    config = load_merged_config(_project(tmp_path, _GRANTS, tracked=True))

    assert config.llm_allow_api is True
    assert config.custom_base_url == "http://localhost:8000/v1"
    assert config.custom_api_key_env == "CUSTOM_LLM_API_KEY"


def test_tracked_config_may_still_restrict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed ``llm_allow_api: false`` keeps overriding a permissive global."""
    global_path = tmp_path / "global.yaml"
    global_path.write_text("llm_allow_api: true\n", encoding="utf-8")
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))

    config = load_merged_config(
        _project(tmp_path, "llm_allow_api: false\n", tracked=True)
    )

    assert config.llm_allow_api is False
    assert config.repository_controlled_keys_ignored == ()
