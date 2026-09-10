"""Fail-closed tests for the pi sandbox allowlist + launcher (M1 rework).

Exercises scripts/pi-sandbox-policy.mjs and the node launcher
(scripts/pi-sandbox-launch.mjs, via the pi-sandbox-run.sh shim) as
subprocesses. A fake pi executable records argv, cwd, stdin, and env so the
REAL launch path is covered — not just dry runs. All hermetic; no network.
"""

from __future__ import annotations

import hashlib
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

FAKE_PI = """#!/bin/sh
outdir="$FAKE_PI_OUT"
printf '%s\\n' "$@" > "$outdir/argv"
[ -n "$FAKE_EXIT" ] && exit "$FAKE_EXIT"
cat > "$outdir/stdin" 2>/dev/null || true
pwd > "$outdir/cwd"
env | sort > "$outdir/env"
# record the staged extension bytes the -e flag points at (what pi would load)
prev=""
for arg in "$@"; do
  if [ "$prev" = "-e" ] && [ -f "$arg" ]; then
    cat "$arg" > "$outdir/staged-ext"
  fi
  prev="$arg"
done
exit 0
"""


@pytest.fixture()
def fake_pi(tmp_path: Path) -> Path:
    """An executable fake pi that records argv/stdin/cwd/env into a directory."""
    outdir = tmp_path / "fake-pi-out"
    outdir.mkdir()
    script = tmp_path / "fake-pi"
    script.write_text(FAKE_PI.replace("$FAKE_PI_OUT", str(outdir)))
    script.chmod(0o755)
    (tmp_path / "invocations").write_text("0")
    return script


def run_policy(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(POLICY), *args], capture_output=True, text=True, timeout=60
    )


def run_launcher(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(__import__("os").environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["sh", str(LAUNCHER), *args], capture_output=True, text=True, timeout=120, env=env
    )


def write_allowlist(tmp_path: Path, mutate) -> Path:
    doc = json.loads(ALLOWLIST.read_text())
    mutate(doc)
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps(doc, indent=2))
    return path


# --- bundled policy is valid ---------------------------------------------------


def test_bundled_allowlist_validates() -> None:
    r = run_policy("validate", str(ALLOWLIST))
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


# --- structural validation fails closed -----------------------------------------


def test_validate_rejects_npm_and_git_pins(tmp_path: Path) -> None:
    for source in ("npm:pi-summerize@1.0.0", "git:fakoli/pi-extensions"):
        path = write_allowlist(tmp_path, lambda d, s=source: d["profiles"]["unattended-exec"].__setitem__(
            "extensions", [{"source": s, "sha256": "0" * 64}]
        ))
        r = run_policy("validate", str(path))
        assert r.returncode == 2, source
        assert "npm:/git: entries are rejected" in r.stderr


def test_validate_rejects_missing_sha256(tmp_path: Path) -> None:
    path = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "extensions", [{"source": "path:/etc/hosts"}]
    ))
    r = run_policy("validate", str(path))
    assert r.returncode == 2
    assert "sha256" in r.stderr


def test_validate_rejects_unknown_root_key(tmp_path: Path) -> None:
    path = write_allowlist(tmp_path, lambda d: d.__setitem__("defaults", {}))
    r = run_policy("validate", str(path))
    assert r.returncode == 2
    assert 'unknown root key "defaults"' in r.stderr


def test_validate_rejects_projecttrust(tmp_path: Path) -> None:
    path = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "projectTrust", "never"
    ))
    r = run_policy("validate", str(path))
    assert r.returncode == 2
    assert "projectTrust" in r.stderr


def test_validate_rejects_nonempty_skills(tmp_path: Path) -> None:
    path = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "skills", ["some-skill"]
    ))
    r = run_policy("validate", str(path))
    assert r.returncode == 2
    assert "skills" in r.stderr


def test_validate_rejects_bad_network_and_unknown_profile_key(tmp_path: Path) -> None:
    path = write_allowlist(tmp_path, lambda d: (
        d["profiles"]["unattended-exec"].__setitem__("network", "unrestricted"),
        d["profiles"]["unattended-exec"].__setitem__("shell", "/bin/sh"),
    ))
    r = run_policy("validate", str(path))
    assert r.returncode == 2
    assert "network" in r.stderr and 'unknown key "shell"' in r.stderr


# --- staging: verified bytes are the loaded bytes --------------------------------


def _pin_entry(tmp_path: Path, extra_files: dict[str, str] | None = None) -> tuple[Path, str]:
    """Create an extension entry file (+ optional siblings) and its sha256 pin."""
    ext_dir = tmp_path / "ext"
    ext_dir.mkdir(exist_ok=True)
    entry = ext_dir / "entry.ts"
    entry.write_text("export default function () {}\n")
    for name, content in (extra_files or {}).items():
        (ext_dir / name).write_text(content)
    digest = hashlib.sha256(entry.read_bytes()).hexdigest()
    return entry, digest


def test_launcher_stages_and_loads_staged_bytes(tmp_path: Path, fake_pi: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    allowlist_dir = tmp_path / "policy"  # allowlist OUTSIDE the workspace
    allowlist_dir.mkdir()
    entry, digest = _pin_entry(allowlist_dir)
    allowlist = write_allowlist(allowlist_dir, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "extensions", [{"source": f"path:ext/{entry.name}", "sha256": digest}]
    ))
    # malicious same-named file in the workspace must be irrelevant
    (workspace / "entry.ts").write_text("malicious()\n")

    r = run_launcher(
        "--profile", "unattended-exec",
        "--workspace", str(workspace),
        "--task", "hello",
        "--allowlist", str(allowlist),
        "--pi", str(fake_pi),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "STAGED" in r.stdout
    argv = (tmp_path / "fake-pi-out" / "argv").read_text().splitlines()
    staged = [a for a in argv if a.endswith("entry.ts")]
    assert staged, argv
    assert staged[0].startswith("/tmp/pi-sandbox-stage.")
    assert "ws/entry.ts" not in staged[0] and "policy/ext/entry.ts" not in staged[0]
    loaded = (tmp_path / "fake-pi-out" / "staged-ext").read_text()
    assert loaded == "export default function () {}\n"


def test_launcher_rejects_symlinked_pin(tmp_path: Path, fake_pi: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    real = tmp_path / "real-entry.ts"
    real.write_text("export default 1\n")
    link = tmp_path / "link-entry.ts"
    link.symlink_to(real)
    allowlist = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "extensions", [{"source": f"path:{link}", "sha256": "0" * 64}]
    ))
    r = run_launcher(
        "--profile", "unattended-exec", "--workspace", str(workspace),
        "--task", "x", "--allowlist", str(allowlist), "--pi", str(fake_pi),
    )
    assert r.returncode == 3
    assert "symlink" in (r.stdout + r.stderr)


def test_launcher_relative_pin_resolves_against_allowlist_dir(tmp_path: Path, fake_pi: Path) -> None:
    """The classic bypass: approved ext next to the policy, malicious ext in cwd."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "ext.ts").write_text("malicious()\n")
    approved = tmp_path / "ext.ts"
    approved.write_text("export default 1\n")
    digest = hashlib.sha256(approved.read_bytes()).hexdigest()
    allowlist = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "extensions", [{"source": "path:ext.ts", "sha256": digest}]
    ))
    # run with cwd = workspace (the launcher must NOT resolve the pin there)
    r = subprocess.run(
        ["sh", str(LAUNCHER), "--profile", "unattended-exec", "--workspace", str(workspace),
         "--task", "x", "--allowlist", str(allowlist), "--pi", str(fake_pi)],
        capture_output=True, text=True, timeout=120, cwd=str(workspace),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    argv = (tmp_path / "fake-pi-out" / "argv").read_text().splitlines()
    staged = [a for a in argv if a.endswith("ext.ts")]
    assert staged and "malicious" not in (tmp_path / "fake-pi-out" / "staged-ext").read_text()


# --- task transport: stdin JSONL, never argv ---------------------------------------


def test_task_travels_via_stdin_and_survives_special_characters(tmp_path: Path, fake_pi: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    nasty_task = 'Review $(touch /tmp/pi-sandbox-injected) --approve @/etc/passwd\nline2 "quoted"'
    r = run_launcher(
        "--profile", "read-only-review", "--workspace", str(workspace),
        "--task", nasty_task, "--allowlist", str(ALLOWLIST), "--pi", str(fake_pi),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    argv = (tmp_path / "fake-pi-out" / "argv").read_text()
    assert "Review $(touch" not in argv  # task is not on argv at all
    stdin = (tmp_path / "fake-pi-out" / "stdin").read_text()
    event = json.loads(stdin.strip().splitlines()[0])
    assert event["type"] == "prompt"
    assert event["message"] == nasty_task  # byte-for-byte, newlines preserved


# --- env hygiene --------------------------------------------------------------------


def test_launcher_scrubs_injection_env_vars(tmp_path: Path, fake_pi: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    r = run_launcher(
        "--profile", "read-only-review", "--workspace", str(workspace),
        "--task", "x", "--allowlist", str(ALLOWLIST), "--pi", str(fake_pi),
        env_extra={"NODE_OPTIONS": "--import=evil.js", "LD_PRELOAD": "/evil.so", "BASH_ENV": "/evil.sh"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    env_text = (tmp_path / "fake-pi-out" / "env").read_text()
    for bad in ("NODE_OPTIONS", "LD_PRELOAD", "BASH_ENV"):
        assert bad not in env_text, env_text


# --- exit codes and aborts ------------------------------------------------------------


def test_launcher_forwards_child_exit_code(tmp_path: Path, fake_pi: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    r = run_launcher(
        "--profile", "read-only-review", "--workspace", str(workspace),
        "--task", "x", "--allowlist", str(ALLOWLIST), "--pi", str(fake_pi),
        env_extra={"FAKE_EXIT": "7"},
    )
    assert r.returncode == 7


def test_launcher_pin_mismatch_aborts_before_launch(tmp_path: Path, fake_pi: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ext, _digest = _pin_entry(tmp_path)
    allowlist = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "extensions", [{"source": f"path:ext/{ext.name}", "sha256": "0" * 64}]
    ))
    r = run_launcher(
        "--profile", "unattended-exec", "--workspace", str(workspace),
        "--task", "x", "--allowlist", str(allowlist), "--pi", str(fake_pi),
    )
    assert r.returncode == 3
    assert "expected sha256" in (r.stdout + r.stderr)
    assert not (tmp_path / "fake-pi-out" / "argv").exists(), "pi must not run on a bad pin"


def test_launcher_staging_error_fails_closed_without_launch(tmp_path: Path, fake_pi: Path) -> None:
    """Any staging error (e.g. unreadable siblings) refuses the run; never a partial launch."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    allowlist = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "extensions", [{"source": "path:/etc/hosts", "sha256": hashlib.sha256(Path("/etc/hosts").read_bytes()).hexdigest()}]
    ))
    r = run_launcher(
        "--profile", "unattended-exec", "--workspace", str(workspace),
        "--task", "x", "--allowlist", str(allowlist), "--pi", str(fake_pi),
    )
    assert r.returncode in (2, 3)
    assert not (tmp_path / "fake-pi-out" / "argv").exists()


def test_launcher_missing_pin_fails_closed(tmp_path: Path, fake_pi: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    allowlist = write_allowlist(tmp_path, lambda d: d["profiles"]["unattended-exec"].__setitem__(
        "extensions", [{"source": "path:/nonexistent/ext.ts", "sha256": "0" * 64}]
    ))
    r = run_launcher(
        "--profile", "unattended-exec", "--workspace", str(workspace),
        "--task", "x", "--allowlist", str(allowlist), "--pi", str(fake_pi),
    )
    assert r.returncode == 2
    assert not (tmp_path / "fake-pi-out" / "argv").exists()


def test_launcher_no_duplicate_executable_in_argv(tmp_path: Path, fake_pi: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    r = run_launcher(
        "--profile", "read-only-review", "--workspace", str(workspace),
        "--task", "x", "--allowlist", str(ALLOWLIST), "--pi", str(fake_pi),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    argv = (tmp_path / "fake-pi-out" / "argv").read_text().splitlines()
    assert argv[0] != "pi", argv  # the composed argv must not start with the executable name
    assert "--no-extensions" in argv


def test_launcher_no_stray_temp_dirs_after_run(tmp_path: Path, fake_pi: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    before = set(Path("/tmp").glob("pi-sandbox-*"))
    r = run_launcher(
        "--profile", "read-only-review", "--workspace", str(workspace),
        "--task", "x", "--allowlist", str(ALLOWLIST), "--pi", str(fake_pi),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    after = set(Path("/tmp").glob("pi-sandbox-*")) - before
    assert not after, f"leaked: {after}"


# --- dry run ------------------------------------------------------------------------


def test_dry_run_reports_without_launching_or_dirs(tmp_path: Path, fake_pi: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    before = set(Path("/tmp").glob("pi-sandbox-*"))
    r = run_launcher(
        "--profile", "unattended-exec", "--workspace", str(workspace),
        "--task", "x", "--allowlist", str(ALLOWLIST), "--pi", str(fake_pi), "--dry-run",
    )
    assert r.returncode == 0, r.stderr
    assert "not launching" in r.stdout
    assert '"--mode"' in r.stdout or "--mode" in r.stdout
    assert not (tmp_path / "fake-pi-out" / "argv").exists()
    assert not set(Path("/tmp").glob("pi-sandbox-*")) - before


# --- compose report --------------------------------------------------------------------


def test_compose_report_shape() -> None:
    r = run_policy("compose", str(ALLOWLIST), "--profile", "read-only-review", "--task", "review it")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    assert report["argv"][0] == "--no-extensions"  # executable NOT duplicated into args
    assert "-na" in report["argv"]
    assert report["taskChars"] == len("review it")
    assert "NODE_OPTIONS" in report["env"]["scrubbed"]


def test_compose_empty_task_fails_closed(tmp_path: Path) -> None:
    task_file = tmp_path / "task.txt"
    task_file.write_text("   \n")
    r = run_policy("compose", str(ALLOWLIST), "--profile", "unattended-exec", "--task-file", str(task_file))
    assert r.returncode == 2