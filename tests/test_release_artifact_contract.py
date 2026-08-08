"""Release-tree skills must match wheel and sdist-built-wheel surfaces."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from anvil import __version__

_REPO = Path(__file__).resolve().parents[1]


def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 180,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _install_wheel(
    wheel: Path, root: Path
) -> tuple[Path, Path, dict[str, str]]:
    venv = root / "artifact-venv"
    created = _run(
        ["uv", "venv", str(venv), "--python", sys.executable], cwd=root
    )
    assert created.returncode == 0, (created.stdout + created.stderr)[-1200:]
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    anvil = scripts / ("anvil.exe" if os.name == "nt" else "anvil")
    installed = _run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)], cwd=root
    )
    assert installed.returncode == 0, (installed.stdout + installed.stderr)[-1600:]

    clean_env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        clean_env.pop(name, None)
    clean_env["PYTHONUTF8"] = "1"
    clean_env["ANVIL_STATE_LAYOUT"] = "local"
    return python, anvil, clean_env


def _qualify_wheel(wheel: Path, root: Path) -> dict[str, Any]:
    root.mkdir(parents=True)
    python, anvil, clean_env = _install_wheel(wheel, root)
    described = _run(
        [str(anvil), "describe", "--json"], cwd=root, env=clean_env
    )
    assert described.returncode == 0, (described.stdout + described.stderr)[-1600:]
    payload = json.loads(described.stdout)
    data = payload["data"]

    version = _run([str(anvil), "--version"], cwd=root, env=clean_env)
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip().startswith(
        f"anvil {data['display_version']} (schema "
    )
    assert data["engine_version"] == __version__
    assert data["build_kind"] in {"source_artifact", "artifact_unknown"}
    assert data["display_version"].startswith(f"{__version__}+")
    if data["build_kind"] == "source_artifact":
        assert isinstance(data["commit"], str)
        assert data["tag"] is None or data["tag"].startswith("v")
        assert data["tag_distance"] is None or data["tag_distance"] >= 0
    else:
        assert data["commit"] is None
        assert data["tag"] is None
        assert data["tag_distance"] is None
    assert isinstance(data["dirty"], bool)
    assert data["cli"]["count"] > 20
    assert data["cli"]["contract_count"] > data["cli"]["count"]
    assert "prd source-name" in data["cli"]["commands"]
    assert "--prd" in data["cli"]["options"]["prd source-name"]

    manifest = root / "installed-describe.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    checked = _run(
        [
            sys.executable,
            str(_REPO / "scripts" / "check_skill_cli_contract.py"),
            "--manifest",
            str(manifest),
            "--repo-root",
            str(_REPO),
            "--expected-version",
            __version__,
            "--expected-api-version",
            "7",
        ],
        cwd=root,
        env=clean_env,
    )
    assert checked.returncode == 0, (checked.stdout + checked.stderr)[-2400:]
    assert "skill/CLI contract: PASS" in checked.stdout

    release_checked = _run(
        [
            sys.executable,
            str(_REPO / "scripts" / "check_release_contract.py"),
            "--manifest",
            str(manifest),
            "--repo-root",
            str(_REPO),
        ],
        cwd=root,
        env=clean_env,
    )
    assert release_checked.returncode == 0, (
        release_checked.stdout + release_checked.stderr
    )[-2400:]

    project = root / "project"
    project.mkdir()
    initialized = _run(
        [str(anvil), "init", "--name", "Artifact Contract"],
        cwd=project,
        env=clean_env,
    )
    assert initialized.returncode == 0, initialized.stderr
    source_name = _run(
        [str(anvil), "prd", "source-name", "--prd", "CON", "--json"],
        cwd=project,
        env=clean_env,
    )
    assert source_name.returncode == 0, source_name.stderr
    source_payload = json.loads(source_name.stdout)
    relative_name = source_payload["data"]["relative_name"]
    assert source_payload["ok"] is True
    assert relative_name.startswith("prds/") and relative_name.endswith(".md")
    assert not Path(relative_name).is_absolute()

    mcp = root / "artifact-venv" / (
        "Scripts/anvil-mcp.exe" if os.name == "nt" else "bin/anvil-mcp"
    )
    mcp_version = _run([str(mcp), "--version"], cwd=root, env=clean_env)
    assert mcp_version.returncode == 0, mcp_version.stderr
    assert mcp_version.stdout.strip() == data["display_version"]
    return data


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required")
def test_built_distributions_match_shipped_skill_contract(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    built = _run(
        ["uv", "build", "--sdist", "--wheel", "--out-dir", str(dist)],
        cwd=_REPO / "bin",
    )
    assert built.returncode == 0, (built.stdout + built.stderr)[-1600:]
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))

    rebuilt_dir = tmp_path / "rebuilt"
    rebuilt = _run(
        ["uv", "build", "--wheel", "--out-dir", str(rebuilt_dir), str(sdist)],
        cwd=tmp_path,
    )
    assert rebuilt.returncode == 0, (rebuilt.stdout + rebuilt.stderr)[-1600:]
    rebuilt_wheel = next(rebuilt_dir.glob("*.whl"))

    direct_identity = _qualify_wheel(wheel, tmp_path / "direct")
    rebuilt_identity = _qualify_wheel(rebuilt_wheel, tmp_path / "from-sdist")
    assert rebuilt_identity == direct_identity
