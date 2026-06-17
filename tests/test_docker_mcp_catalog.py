"""Docker MCP catalog packaging gate (backlog T021 / feature F006).

T021 publishes the FastMCP stdio server to the Docker MCP catalog. That requires
three artifacts to exist and agree:

  1. A repo-root ``Dockerfile`` that packages ``bin/src/fakoli_state`` and starts
     the stdio MCP server, with ``FAKOLI_STATE_ROOT`` bind-mount support.
  2. A repo-root ``server.yaml`` Docker MCP catalog manifest.
  3. A documented publishing path in ``docs/mcp.md``.

The acceptance criteria also require the image to start the server *and* a smoke
test that "connects and lists tools". The backlog verification command is::

    docker build -t fakoli-state-mcp plugins/fakoli-state \
        && docker run --rm fakoli-state-mcp --help

In this standalone repo the build context is the repo root (drop the
``plugins/fakoli-state/`` prefix). The load-bearing prerequisite for that smoke
test is that the server entry point handles ``--help``/``--version`` and exits 0
*without* blocking on stdio — otherwise ``docker run ... --help`` hangs forever.
``mcp.run()`` ignores argv, so before T021 there was no such handler.

This module enforces all of that. The entry-point checks run in-process (fast,
hermetic, always exercised in CI). The actual ``docker build``/``docker run``
checks are guarded behind a Docker-availability skip so the suite stays green on
machines and CI runners without a Docker daemon.

Layout note: this test lives at ``<repo-root>/tests/`` and the artifacts live at
``<repo-root>/``; ``parents[1]`` is the repo root — matching
``test_standalone_docs.py`` and ``test_version_sync.py``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from fakoli_state import __version__
from fakoli_state.cli.describe import mcp_tool_names
from fakoli_state.mcp_server import main

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """``<root>/tests/test_docker_mcp_catalog.py`` → ``parents[1]`` is the root."""
    return Path(__file__).resolve().parents[1]


def _dockerfile() -> Path:
    return _repo_root() / "Dockerfile"


def _server_yaml() -> Path:
    return _repo_root() / "server.yaml"


def _dockerignore() -> Path:
    return _repo_root() / ".dockerignore"


def _mcp_doc() -> Path:
    return _repo_root() / "docs" / "mcp.md"


# ---------------------------------------------------------------------------
# Entry-point smoke test (in-process — the contract the Docker smoke test relies on)
# ---------------------------------------------------------------------------


class TestEntryPointFlags:
    """``python -m fakoli_state.mcp_server --help/--version`` must not block."""

    def test_help_prints_and_returns_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["--help"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "usage:" in out
        # The help page must document the bind-mount env var (acceptance criterion).
        assert "FAKOLI_STATE_ROOT" in out

    def test_help_lists_every_registered_tool(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # "connects and lists tools" — the --help surface is introspected live
        # from the FastMCP server, so every registered tool must appear.
        rc = main(["--help"])
        out = capsys.readouterr().out
        assert rc == 0
        tools = mcp_tool_names()
        assert tools, "expected at least one registered MCP tool"
        for tool in tools:
            assert tool in out, f"tool {tool!r} missing from --help output"

    def test_short_help_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["-h"]) == 0
        assert "usage:" in capsys.readouterr().out

    def test_version_prints_engine_version(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["--version"])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.strip() == __version__

    def test_short_version_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["-v"]) == 0
        assert capsys.readouterr().out.strip() == __version__

    def test_unknown_flag_fails_fast(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A typo'd flag must NOT silently start the (blocking) server.
        rc = main(["--definitely-not-a-flag"])
        err = capsys.readouterr().err
        assert rc == 2
        assert "unrecognized" in err

    def test_no_args_starts_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Backward-compatibility: the default (no-arg) path must still call
        # mcp.run(). We stub run() so the test does not block on stdio.
        import fakoli_state.mcp_server as srv

        called: dict[str, bool] = {"ran": False}

        def _fake_run() -> None:
            called["ran"] = True

        monkeypatch.setattr(srv.mcp, "run", _fake_run)
        rc = main([])
        assert rc == 0
        assert called["ran"] is True


# ---------------------------------------------------------------------------
# Dockerfile structure
# ---------------------------------------------------------------------------


class TestDockerfile:
    def test_dockerfile_exists(self) -> None:
        assert _dockerfile().is_file(), "repo-root Dockerfile is required for T021"

    def test_dockerfile_runs_the_mcp_server_module(self) -> None:
        text = _dockerfile().read_text()
        # The image must start the FastMCP stdio server.
        assert "fakoli_state.mcp_server" in text

    def test_dockerfile_documents_bind_mount_root(self) -> None:
        text = _dockerfile().read_text()
        # FAKOLI_STATE_ROOT bind-mount support is an explicit acceptance criterion.
        assert "FAKOLI_STATE_ROOT" in text

    def test_dockerfile_installs_from_lockfile(self) -> None:
        # Reproducible image: install from the committed uv.lock, not a fresh resolve.
        text = _dockerfile().read_text()
        assert "uv sync" in text
        assert "--frozen" in text

    def test_dockerignore_exists_and_excludes_venv(self) -> None:
        assert _dockerignore().is_file()
        text = _dockerignore().read_text()
        assert "bin/.venv/" in text


# ---------------------------------------------------------------------------
# Catalog manifest (server.yaml)
# ---------------------------------------------------------------------------


class TestCatalogManifest:
    def test_server_yaml_exists_and_parses(self) -> None:
        assert _server_yaml().is_file(), "server.yaml catalog manifest is required"
        data = yaml.safe_load(_server_yaml().read_text())
        assert isinstance(data, dict)

    def test_required_top_level_fields(self) -> None:
        data = yaml.safe_load(_server_yaml().read_text())
        # Docker MCP registry server.yaml required keys.
        for key in ("name", "image", "type", "meta", "about", "source"):
            assert key in data, f"server.yaml missing required key {key!r}"
        assert data["name"] == "fakoli-state"
        assert data["type"] == "server"

    def test_source_points_at_local_dockerfile(self) -> None:
        data = yaml.safe_load(_server_yaml().read_text())
        source = data["source"]
        # The image builds from the repo-root Dockerfile (no pre-published image).
        assert source.get("dockerfile") == "Dockerfile"
        assert "project" in source

    def test_manifest_wires_bind_mount(self) -> None:
        data = yaml.safe_load(_server_yaml().read_text())
        # A volume + FAKOLI_STATE_ROOT env must be declared so catalog users get
        # the bind-mount automatically.
        run = data.get("run", {})
        volumes = run.get("volumes", [])
        assert any("/project" in v for v in volumes), "expected a /project volume mapping"

        config = data.get("config", {})
        env_names = {e.get("name") for e in config.get("env", [])}
        assert "FAKOLI_STATE_ROOT" in env_names

    def test_meta_category_is_valid(self) -> None:
        data = yaml.safe_load(_server_yaml().read_text())
        # Category mirrors the marketplace taxonomy used elsewhere in the repo.
        assert data["meta"]["category"] in {"productivity", "integrations", "utilities"}


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


class TestPublishingDocs:
    def test_mcp_doc_documents_publishing(self) -> None:
        text = _mcp_doc().read_text()
        assert "Docker MCP catalog" in text
        # The smoke-test build command must be documented.
        assert "docker build -t fakoli-state-mcp" in text
        # The bind-mount run convention must be documented.
        assert "FAKOLI_STATE_ROOT" in text


# ---------------------------------------------------------------------------
# Real Docker build + smoke test (skips gracefully when Docker is unavailable)
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        proc = subprocess.run(
            [docker, "info"],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


requires_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="docker daemon not available; CI-safe skip",
)


@requires_docker
class TestDockerBuildSmoke:
    """Mirror the backlog verification: build the image, run `--help`.

    Skipped automatically when no Docker daemon is reachable, so this never
    breaks a CI runner or laptop without Docker.
    """

    def test_build_and_help(self) -> None:
        root = _repo_root()
        tag = "fakoli-state-mcp:t021-test"
        build = subprocess.run(
            ["docker", "build", "-t", tag, "."],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert build.returncode == 0, f"docker build failed:\n{build.stderr}"

        try:
            run = subprocess.run(
                ["docker", "run", "--rm", tag, "--help"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert run.returncode == 0, f"docker run --help failed:\n{run.stderr}"
            assert "usage:" in run.stdout
            # The smoke test must list tools.
            for tool in mcp_tool_names():
                assert tool in run.stdout
        finally:
            subprocess.run(
                ["docker", "image", "rm", "-f", tag],
                capture_output=True,
                timeout=60,
            )
