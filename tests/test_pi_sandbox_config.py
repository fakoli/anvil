"""Fail-closed tests for the sandbox run-config (docker-path knobs).

Exercises scripts/pi-sandbox-config.mjs directly and scripts/pi-sandbox-docker.sh
end-to-end with a fake docker on PATH (real node + real policy module in the
loop — the sh parsing/composition contract is covered, not stubbed).
All hermetic; no network, no real docker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_MJS = REPO_ROOT / "scripts" / "pi-sandbox-config.mjs"
DOCKER_SH = REPO_ROOT / "scripts" / "pi-sandbox-docker.sh"
ALLOWLIST = REPO_ROOT / "packaging" / "pi" / "sandbox" / "allowlist.json"
DIGEST = "sha256:" + "a" * 64
DIGEST_IMAGE = f"ghcr.io/fakoli/anvil-pi-sandbox@{DIGEST}"

pytestmark = [
    pytest.mark.skipif(
        shutil.which("node") is None or shutil.which("sh") is None,
        reason="node + POSIX sh are required for the sandbox config contract",
    ),
    pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="pi-sandbox-docker.sh refuses to run as root by design",
    ),
]


def run_config(*args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = str(home)
    return subprocess.run(
        ["node", str(CONFIG_MJS), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def resolve_ok(*args: str, home: Path | None = None) -> dict[str, str]:
    r = run_config("resolve", *args, home=home)
    assert r.returncode == 0, f"expected ok, got {r.returncode}: {r.stderr}"
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        key, _, val = line.partition("\t")
        out[key] = val
    return out


@pytest.fixture()
def sandbox_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "anvil").mkdir(parents=True)
    return home


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


FAKE_DOCKER = """#!/bin/sh
outdir="$FAKE_DOCKER_OUT"
if [ "$1" = "ps" ]; then
  [ -n "$FAKE_PS_IDS" ] && printf '%s\\n' "$FAKE_PS_IDS"
  exit 0
fi
if [ "$1" = "run" ]; then
  printf '%s\\n' "$@" > "$outdir/run-argv"
  exit 0
fi
if [ "$1" = "build" ]; then
  printf '%s\\n' "$@" > "$outdir/build-argv"
  exit 0
fi
exit 0
"""


@pytest.fixture()
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    outdir = tmp_path / "fake-docker-out"
    outdir.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text(FAKE_DOCKER.replace("$FAKE_DOCKER_OUT", str(outdir)))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_OUT", str(outdir))
    return outdir


def run_docker(
    *args: str, home: Path | None = None, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["sh", str(DOCKER_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# ---- config module: resolution ---------------------------------------------


def test_resolve_defaults(sandbox_home: Path, workspace: Path) -> None:
    out = resolve_ok(
        "--profile", "unattended-exec",
        "--workspace", str(workspace),
        "--allowlist", str(ALLOWLIST),
        home=sandbox_home,
    )
    assert out["IMAGE"] == ""
    assert out["NETWORK"] == "none"
    assert out["CAPS"] == "all-dropped"
    assert out["MAX_CONTAINERS"] == ""
    assert out["CONFIG_SOURCE"] == "defaults"


def test_user_config_applies(sandbox_home: Path, workspace: Path) -> None:
    user_file = sandbox_home / ".config" / "anvil" / "sandbox.config.json"
    user_file.write_text(json.dumps({"image": DIGEST_IMAGE, "caps": "docker-default", "max_containers": 3}))
    out = resolve_ok(
        "--profile", "unattended-exec",
        "--workspace", str(workspace),
        "--allowlist", str(ALLOWLIST),
        home=sandbox_home,
    )
    assert out["IMAGE"] == DIGEST_IMAGE
    assert out["CAPS"] == "docker-default"
    assert out["MAX_CONTAINERS"] == "3"
    assert out["CONFIG_SOURCE"] == str(user_file)


def test_explicit_config_beats_user(sandbox_home: Path, workspace: Path, tmp_path: Path) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"caps": "docker-default"}))
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"caps": "all-dropped"}))
    out = resolve_ok(
        "--profile", "unattended-exec",
        "--workspace", str(workspace),
        "--allowlist", str(ALLOWLIST),
        "--config", str(explicit),
        home=sandbox_home,
    )
    assert out["CAPS"] == "all-dropped"
    assert out["CONFIG_SOURCE"] == str(explicit)


def test_project_max_containers_most_restrictive_wins(sandbox_home: Path, tmp_path: Path) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"max_containers": 4}))
    ws_low = tmp_path / "ws-low"
    (ws_low / ".pi").mkdir(parents=True)
    (ws_low / ".pi" / "sandbox.config.json").write_text(json.dumps({"max_containers": 2}))
    ws_high = tmp_path / "ws-high"
    (ws_high / ".pi").mkdir(parents=True)
    (ws_high / ".pi" / "sandbox.config.json").write_text(json.dumps({"max_containers": 9}))
    base = ["--profile", "unattended-exec", "--allowlist", str(ALLOWLIST), "--workspace"]
    out_low = resolve_ok(*base, str(ws_low), home=sandbox_home)
    out_high = resolve_ok(*base, str(ws_high), home=sandbox_home)
    assert out_low["MAX_CONTAINERS"] == "2"
    assert out_high["MAX_CONTAINERS"] == "4"


# ---- config module: fail-closed validation ---------------------------------


def test_project_security_fields_refused(sandbox_home: Path, workspace: Path) -> None:
    (workspace / ".pi").mkdir()
    (workspace / ".pi" / "sandbox.config.json").write_text(
        json.dumps({"image": DIGEST_IMAGE, "network": "none", "caps": "all-dropped", "max_containers": 2})
    )
    r = run_config(
        "resolve",
        "--profile", "unattended-exec",
        "--workspace", str(workspace),
        "--allowlist", str(ALLOWLIST),
        home=sandbox_home,
    )
    assert r.returncode == 2
    assert "trusted-scope" in r.stderr


def test_unknown_key_refused(sandbox_home: Path, workspace: Path) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"volumes": ["/etc"]}))

    r = run_config(
        "resolve",
        "--profile", "unattended-exec",
        "--workspace", str(workspace),
        "--allowlist", str(ALLOWLIST),
        home=sandbox_home,
    )
    assert r.returncode == 2
    assert 'unknown key "volumes"' in r.stderr


def test_image_must_be_digest_pinned(sandbox_home: Path, workspace: Path) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"image": "anvil-pi-sandbox:latest"}))
    r = run_config(
        "resolve",
        "--profile", "unattended-exec",
        "--workspace", str(workspace),
        "--allowlist", str(ALLOWLIST),
        home=sandbox_home,
    )
    assert r.returncode == 3
    assert "digest-pinned" in r.stderr

    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"image": "--privileged"}))
    r2 = run_config(
        "resolve",
        "--profile", "unattended-exec",
        "--workspace", str(workspace),
        "--allowlist", str(ALLOWLIST),
        home=sandbox_home,
    )
    assert r2.returncode == 3


def test_network_inference_rejected(sandbox_home: Path, workspace: Path) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"network": "inference"}))
    r = run_config(
        "resolve",
        "--profile", "unattended-exec",
        "--workspace", str(workspace),
        "--allowlist", str(ALLOWLIST),
        home=sandbox_home,
    )
    assert r.returncode == 3
    assert "reserved" in r.stderr


@pytest.mark.parametrize("caps", ["keep-net-raw", "ALL", "", None])
def test_caps_preset_validation(sandbox_home: Path, workspace: Path, caps: str | None) -> None:
    doc: dict[str, str] = {} if caps is None else {"caps": caps}
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps(doc))
    r = run_config(
        "resolve",
        "--profile", "unattended-exec",
        "--workspace", str(workspace),
        "--allowlist", str(ALLOWLIST),
        home=sandbox_home,
    )
    assert r.returncode == (0 if caps is None else 3)
    if caps is not None:
        assert '"caps" must be one of' in r.stderr


@pytest.mark.parametrize("value,ok", [(0, False), (257, False), ("3", False), (1, True), (256, True)])
def test_max_containers_bounds(sandbox_home: Path, workspace: Path, value: int | str, ok: bool) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"max_containers": value}))
    r = run_config(
        "resolve",
        "--profile", "unattended-exec",
        "--workspace", str(workspace),
        "--allowlist", str(ALLOWLIST),
        home=sandbox_home,
    )
    assert (r.returncode == 0) is ok
    if not ok:
        assert '"max_containers" must be an integer' in r.stderr


def test_malformed_json_is_struct_error(sandbox_home: Path, workspace: Path) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text("{not json")
    r = run_config(
        "resolve",
        "--profile", "unattended-exec",
        "--workspace", str(workspace),
        "--allowlist", str(ALLOWLIST),
        home=sandbox_home,
    )
    assert r.returncode == 3


def test_validate_verb(sandbox_home: Path, tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"caps": "docker-default"}))
    r = run_config("validate", "--config", str(good), "--allowlist", str(ALLOWLIST), "--profile", "unattended-exec", home=sandbox_home)
    assert r.returncode == 0 and r.stdout.startswith("OK\t")

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"image": "unpinned"}))
    r2 = run_config("validate", "--config", str(bad), "--allowlist", str(ALLOWLIST), "--profile", "unattended-exec", home=sandbox_home)
    assert r2.returncode == 3


def test_missing_profile_refused(sandbox_home: Path, workspace: Path) -> None:
    r = run_config(
        "resolve",
        "--profile", "no-such-profile",
        "--workspace", str(workspace),
        "--allowlist", str(ALLOWLIST),
        home=sandbox_home,
    )
    assert r.returncode == 2


# ---- docker wrapper integration (fake docker, real node) -------------------


def docker_run_argv(fake_docker: Path) -> list[str]:
    return (fake_docker / "run-argv").read_text().splitlines()


def test_docker_compose_defaults(fake_docker: Path, sandbox_home: Path, workspace: Path) -> None:
    task = workspace / "task.txt"
    task.write_text("hi")
    r = run_docker("unattended-exec", str(task), str(workspace), home=sandbox_home)
    assert r.returncode == 0, r.stderr
    argv = docker_run_argv(fake_docker)
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--label" in argv and argv[argv.index("--label") + 1] == "anvil.sandbox=pi-sandbox"
    assert argv[-3] == "anvil-pi-sandbox"
    assert argv[-2] == "unattended-exec"
    assert argv[-1] == "/task/task.txt"
    assert "source=defaults" in r.stderr


def test_docker_caps_preset_drops_flag(fake_docker: Path, sandbox_home: Path, workspace: Path) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"caps": "docker-default"}))
    task = workspace / "task.txt"
    task.write_text("hi")
    r = run_docker("unattended-exec", str(task), str(workspace), home=sandbox_home)
    assert r.returncode == 0, r.stderr
    argv = docker_run_argv(fake_docker)
    assert "--cap-drop" not in argv
    assert "--network" in argv  # network posture is never relaxed by the caps preset


def test_docker_env_image_beats_config(fake_docker: Path, sandbox_home: Path, workspace: Path) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"image": DIGEST_IMAGE}))
    task = workspace / "task.txt"
    task.write_text("hi")
    r = run_docker("unattended-exec", str(task), str(workspace), home=sandbox_home, extra_env={"ANVIL_SANDBOX_IMAGE": "local-tag"})
    assert r.returncode == 0, r.stderr
    assert docker_run_argv(fake_docker)[-3] == "local-tag"


def test_docker_config_image_used(fake_docker: Path, sandbox_home: Path, workspace: Path) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"image": DIGEST_IMAGE}))
    task = workspace / "task.txt"
    task.write_text("hi")
    r = run_docker("unattended-exec", str(task), str(workspace), home=sandbox_home)
    assert r.returncode == 0, r.stderr
    assert docker_run_argv(fake_docker)[-3] == DIGEST_IMAGE


def test_docker_max_containers_refuses(fake_docker: Path, sandbox_home: Path, workspace: Path) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"max_containers": 2}))
    task = workspace / "task.txt"
    task.write_text("hi")
    r = run_docker("unattended-exec", str(task), str(workspace), home=sandbox_home, extra_env={"FAKE_PS_IDS": "c1\nc2"})
    assert r.returncode == 2
    assert "max_containers=2" in r.stderr
    assert not (fake_docker / "run-argv").exists()


def test_docker_max_containers_allows_under(fake_docker: Path, sandbox_home: Path, workspace: Path) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"max_containers": 2}))
    task = workspace / "task.txt"
    task.write_text("hi")
    r = run_docker("unattended-exec", str(task), str(workspace), home=sandbox_home, extra_env={"FAKE_PS_IDS": "c1"})
    assert r.returncode == 0, r.stderr
    assert (fake_docker / "run-argv").exists()


def test_docker_project_scope_feeds_guard(fake_docker: Path, sandbox_home: Path, workspace: Path) -> None:
    (workspace / ".pi").mkdir()
    (workspace / ".pi" / "sandbox.config.json").write_text(json.dumps({"max_containers": 1}))
    task = workspace / "task.txt"
    task.write_text("hi")
    r = run_docker("unattended-exec", str(task), str(workspace), home=sandbox_home, extra_env={"FAKE_PS_IDS": "busy"})
    assert r.returncode == 2
    assert not (fake_docker / "run-argv").exists()


def test_docker_build_refuses_digest_image(fake_docker: Path, sandbox_home: Path, workspace: Path) -> None:
    (sandbox_home / ".config" / "anvil" / "sandbox.config.json").write_text(json.dumps({"image": DIGEST_IMAGE}))
    task = workspace / "task.txt"
    task.write_text("hi")
    r = run_docker("--build", "unattended-exec", str(task), str(workspace), home=sandbox_home)
    assert r.returncode == 2
    assert "digest-pinned" in r.stderr