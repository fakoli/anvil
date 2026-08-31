from __future__ import annotations

import json
from pathlib import Path

import pytest

from anvil import __version__
from anvil.build_identity import BuildIdentity
from anvil.cli import hooks


def _source_identity(*, distance: int, dirty: bool = False) -> BuildIdentity:
    commit = "20bee59c22ff"
    local = f"{distance}.g{commit}" + (".dirty" if dirty else "")
    return BuildIdentity(
        build_kind="source_checkout",
        display_version=f"{__version__}+{local}",
        commit=commit,
        tag=f"v{__version__}",
        tag_distance=distance,
        dirty=dirty,
    )


def _write_manifest(root: Path, version: str) -> None:
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"version": version}), encoding="utf-8")


def test_active_engine_version_keeps_ahead_of_tag_provenance(monkeypatch) -> None:  # noqa: ANN001
    identity = _source_identity(distance=6)
    monkeypatch.setattr(
        "anvil.build_identity.get_build_identity", lambda: identity
    )

    assert hooks._active_engine_version() == identity.display_version


def test_active_engine_version_collapses_exact_clean_release_tag(monkeypatch) -> None:  # noqa: ANN001
    identity = _source_identity(distance=0)
    monkeypatch.setattr(
        "anvil.build_identity.get_build_identity", lambda: identity
    )

    assert hooks._active_engine_version() == __version__


@pytest.mark.parametrize("failure", [PermissionError("denied"), OSError("loop")])
def test_active_engine_version_keeps_context_when_provenance_is_unavailable(
    monkeypatch, failure: OSError
) -> None:  # noqa: ANN001
    def unavailable() -> BuildIdentity:
        raise failure

    monkeypatch.setattr("anvil.build_identity.get_build_identity", unavailable)

    assert hooks._active_engine_version() == f"{__version__}+source.unknown"


def test_plugin_manifest_attaches_matching_source_provenance(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    identity = _source_identity(distance=6)
    _write_manifest(tmp_path, __version__)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "anvil.build_identity.get_build_identity", lambda: identity
    )

    assert hooks._probe_plugin_manifest() == hooks._InstallationProbe(
        "ok", identity.display_version
    )


def test_plugin_manifest_preserves_a_different_release_line(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    _write_manifest(tmp_path, "0.6.4")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "anvil.build_identity.get_build_identity", lambda: _source_identity(distance=6)
    )

    assert hooks._probe_plugin_manifest() == hooks._InstallationProbe("ok", "0.6.4")


def test_source_plugin_and_matching_path_build_are_not_reported_as_skewed(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    identity = _source_identity(distance=6)
    _write_manifest(tmp_path, __version__)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "anvil.build_identity.get_build_identity", lambda: identity
    )
    active = hooks._active_hook_identity()
    monkeypatch.setattr(
        hooks,
        "_probe_path_engine",
        lambda: hooks._InstallationProbe(
            "ok", identity.display_version, active.schema_version
        ),
    )

    context = hooks._render_healthy_installation_context(
        "active-claims:0 ready-tasks:0 blockers:0 prd-status:none",
        "Python",
    )

    assert context == (
        "[anvil] Language: Python | active-claims:0 ready-tasks:0 "
        "blockers:0 prd-status:none"
    )
