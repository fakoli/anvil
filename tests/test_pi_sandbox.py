"""Fail-closed tests for the pi sandbox allowlist + launcher (M1).

These tests exercise scripts/pi-sandbox-policy.mjs and
scripts/pi-sandbox-run.sh as subprocesses, mirroring how the launcher is used
in production. All cases are hermetic (tmp_path); no network. npm/git pin
verification against live registries is deliberately NOT tested here — those
are exercised at image build time (M4) and marked live_* if ever added.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = REPO_ROOT / "packaging" / "pi" / "sandbox" / "allowlist.json"
POLICY = REPO_ROOT / "scripts" / "pi-sandbox-policy.mjs"
LAUNCHER = REPO_ROOT / "scripts" / "pi-sandbox-run.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("sh") is None,
    reason="node + POSIX sh are required for the sandbox launcher contract",
)


def run_policy(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(POLICY), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO_ROOT),
        timeout=60,
    )


def run_launcher(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(LAUNCHER), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def write_allowlist(tmp_path: Path, mutate) -> Path:
    doc = json.loads(ALLOWLIST.read_text())
    mutate(doc)
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps(doc, indent=2))
    return path


# --- bundled policy is valid -------------------------------------------------


def test_bundled_allowlist_validates() -> None:
    r = run_policy("validate", str(ALLOWLIST))
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


# --- structural validation fails closed --------------------------------------


def test_validate_rejects_missing_sha256(tmp_path: Path) -> None:
    path = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "extensions", [{"source": "path:/etc/hosts"}]
    ))
    r = run_policy("validate", str(path))
    assert r.returncode == 2
    assert "sha256" in r.stderr


def test_validate_rejects_unknown_key(tmp_path: Path) -> None:
    path = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "shell", "/bin/sh"
    ))
    r = run_policy("validate", str(path))
    assert r.returncode == 2
    assert 'unknown key "shell"' in r.stderr


def test_validate_rejects_bad_network(tmp_path: Path) -> None:
    path = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "network", "unrestricted"
    ))
    r = run_policy("validate", str(path))
    assert r.returncode == 2


def test_validate_rejects_empty_tools(tmp_path: Path) -> None:
    path = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__("tools", []))
    r = run_policy("validate", str(path))
    assert r.returncode == 2


def test_validate_rejects_git_pin_without_commit(tmp_path: Path) -> None:
    path = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "extensions", [{"source": "git:fakoli/pi-extensions", "ref": "fakoli-v0.4.0"}]
    ))
    r = run_policy("validate", str(path))
    assert r.returncode == 2
    assert "commit" in r.stderr


# --- pin verification ----------------------------------------------------------


def test_pin_verify_detects_sha256_mismatch(tmp_path: Path) -> None:
    real = (tmp_path / "ext.ts")
    real.write_text("export default function () {}\n")
    path = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "extensions", [{"source": f"path:{real}", "sha256": "0" * 64}]
    ))
    r = run_policy("pin-verify", str(path), "--profile", "unattended-exec")
    assert r.returncode == 3
    assert "MISMATCH" in r.stdout


def test_pin_verify_accepts_correct_sha256(tmp_path: Path) -> None:
    import hashlib

    real = tmp_path / "ext.ts"
    real.write_text("export default function () {}\n")
    digest = hashlib.sha256(real.read_bytes()).hexdigest()
    path = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "extensions", [{"source": f"path:{real}", "sha256": digest}]
    ))
    r = run_policy("pin-verify", str(path), "--profile", "unattended-exec")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK:" in r.stdout


def test_pin_verify_missing_file_fails_closed(tmp_path: Path) -> None:
    path = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "extensions", [{"source": "path:/nonexistent/ext.ts", "sha256": "0" * 64}]
    ))
    r = run_policy("pin-verify", str(path), "--profile", "unattended-exec")
    assert r.returncode == 2
    assert "MISSING" in r.stdout


def test_pin_verify_empty_extensions_passes() -> None:
    r = run_policy("pin-verify", str(ALLOWLIST), "--profile", "unattended-exec")
    assert r.returncode == 0, r.stderr


# --- compose strict recipe ------------------------------------------------------


def test_compose_uses_strict_recipe(tmp_path: Path) -> None:
    task_file = tmp_path / "task.txt"
    task_file.write_text("  review the diff  \n")
    r = run_policy("compose", str(ALLOWLIST), "--profile", "read-only-review", "--task-file", str(task_file))
    assert r.returncode == 0, r.stderr
    composed = json.loads(r.stdout)
    argv = composed["argv"]
    assert argv[0] == "pi"
    assert "--no-extensions" in argv
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"
    assert "--no-skills" in argv
    assert "-na" in argv
    assert "--mode" in argv and "json" in argv
    assert argv[argv.index("-p") + 1] == "review the diff"
    assert composed["env"]["PI_CODING_AGENT_DIR"] == "$SANDBOX_AGENT_DIR"
    assert composed["docker"]["network"] == "none"


def test_compose_empty_task_fails_closed(tmp_path: Path) -> None:
    task_file = tmp_path / "task.txt"
    task_file.write_text("   \n")
    r = run_policy("compose", str(ALLOWLIST), "--profile", "unattended-exec", "--task-file", str(task_file))
    assert r.returncode == 2


# --- launcher ---------------------------------------------------------------------


def test_launcher_dry_run_composes_without_launching(tmp_path: Path) -> None:
    r = run_launcher(
        "--profile", "unattended-exec",
        "--workspace", str(tmp_path),
        "--task", "smoke",
        "--dry-run",
    )
    assert r.returncode == 0, r.stderr
    assert "not launching" in r.stdout
    assert '"--no-extensions"' in r.stdout
    assert "--tools" in r.stdout


def test_launcher_fail_closed_unknown_profile(tmp_path: Path) -> None:
    r = run_launcher("--profile", "does-not-exist", "--workspace", str(tmp_path), "--task", "x", "--dry-run")
    assert r.returncode == 2
    assert 'profile "does-not-exist"' in r.stderr


def test_launcher_fail_closed_pin_mismatch(tmp_path: Path) -> None:
    real = tmp_path / "ext.ts"
    real.write_text("export default 1\n")

    def mutate(doc) -> None:
        doc["profiles"]["unattended-exec"]["extensions"] = [{"source": f"path:{real}", "sha256": "0" * 64}]

    allowlist = write_allowlist(tmp_path, mutate)
    r = run_launcher(
        "--profile", "unattended-exec",
        "--workspace", str(tmp_path),
        "--task", "x",
        "--allowlist", str(allowlist),
        "--dry-run",
    )
    assert r.returncode == 3
    assert "MISMATCH" in r.stdout


def test_launcher_requires_workspace(tmp_path: Path) -> None:
    r = run_launcher("--profile", "unattended-exec", "--workspace", "/nonexistent/dir", "--task", "x", "--dry-run")
    assert r.returncode == 2
    assert "workspace not found" in r.stderr