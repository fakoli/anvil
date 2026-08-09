"""Installed-wheel qualification for provider discovery and package resources."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_BIN = _REPO / "bin"
_TYPER_VERSIONS = ("0.13.0", "0.27.0")


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("uv") is None:
        pytest.skip("uv is required for installed-wheel qualification")
    out = tmp_path_factory.mktemp("provider-wheel")
    built = _run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=_BIN,
    )
    assert built.returncode == 0, built.stderr[-1600:]
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


@pytest.mark.parametrize("typer_version", _TYPER_VERSIONS)
def test_installed_provider_discovery_and_resources(
    built_wheel: Path,
    tmp_path: Path,
    typer_version: str,
) -> None:
    venv = tmp_path / "venv"
    created = _run(
        ["uv", "venv", str(venv), "--python", sys.executable],
        cwd=tmp_path,
    )
    assert created.returncode == 0, created.stderr[-1200:]
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    anvil = venv / ("Scripts/anvil.exe" if os.name == "nt" else "bin/anvil")

    installed = _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            str(built_wheel),
            f"typer=={typer_version}",
            "jsonschema>=4,<5",
        ],
        cwd=tmp_path,
        timeout=240,
    )
    assert installed.returncode == 0, installed.stderr[-2000:]

    described = _run([str(anvil), "describe", "--json"], cwd=tmp_path)
    assert described.returncode == 0, described.stderr[-1200:]
    manifest = json.loads(described.stdout)
    assert manifest["ok"] is True
    data = manifest["data"]
    assert data["cli"]["commands"]
    assert "project snapshot" in data["cli"]["commands"]
    assert "prd show" in data["cli"]["commands"]
    operations = data["operation_catalog"]["operations"]
    assert [item["operation_id"] for item in operations] == [
        "state.prd.content",
        "state.project.snapshot",
    ]

    qualified = _run(
        [
            str(python),
            "-c",
            _INSTALLED_RESOURCE_CHECK,
            typer_version,
        ],
        cwd=tmp_path,
    )
    assert qualified.returncode == 0, (qualified.stdout + qualified.stderr)[-2000:]
    report = json.loads(qualified.stdout)
    assert report == {"resources": 11, "typer": typer_version}


_INSTALLED_RESOURCE_CHECK = r"""
import base64
import hashlib
import json
import sys
from importlib.resources import files

import typer
from jsonschema import Draft202012Validator

from anvil.cli.describe import build_manifest

assert typer.__version__ == sys.argv[1]
catalog = build_manifest()["operation_catalog"]
resources = {
    resource
    for operation in catalog["operations"]
    for group in ("schema_resources", "fixture_resources")
    for resource in operation[group].values()
}
for operation in catalog["operations"]:
    for kind in ("input", "output", "error"):
        schema = json.loads(
            files("anvil._data")
            .joinpath(operation["schema_resources"][kind])
            .read_bytes()
        )
        fixture = json.loads(
            files("anvil._data")
            .joinpath(operation["fixture_resources"][kind])
            .read_bytes()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)

vectors = json.loads(
    files("anvil._data")
    .joinpath("contracts/provider-reads/v1/provider-read-digests.v1.json")
    .read_bytes()
)
snapshot = vectors["project_snapshot"]
assert hashlib.sha256(
    base64.b64decode(snapshot["domain_base64"])
    + base64.b64decode(snapshot["canonical_payload_utf8_base64"])
).hexdigest() == snapshot["snapshot_digest"]
content = vectors["prd_content"]
source = base64.b64decode(content["source_utf8_base64"])
assert hashlib.sha256(source).hexdigest() == content["source_digest"]
print(json.dumps({"resources": len(resources), "typer": typer.__version__}))
"""
