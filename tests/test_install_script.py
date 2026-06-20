"""Guard the one-line installer ``scripts/install.sh``.

Cheap, network-free checks: the script is valid POSIX sh, running it with no
harness argument prints usage and exits 2 *before* it touches uv/git/network, and
the opt-in ``--path`` flag links ``anvil`` into a PATH dir without ever clobbering
an existing one.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _script() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "install.sh"


def _run_in_fake_checkout(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run install.sh against a fake in-place checkout, fully offline.

    install.sh detects an in-checkout run (``./bin/anvil`` + ``./.claude-plugin/
    plugin.json``) and uses it directly — no git/network. Stub ``bin/anvil`` makes
    the terminal ``exec anvil install`` a harmless no-op, and a stub ``uv`` on PATH
    satisfies the prerequisite check without depending on a real uv. ``ANVIL_BIN_DIR``
    points the --path symlink at a temp dir we can assert on.
    """
    (tmp_path / "bin").mkdir(exist_ok=True)  # exist_ok: idempotency test re-runs in place
    anvil = tmp_path / "bin" / "anvil"
    anvil.write_text("#!/bin/sh\nexit 0\n")  # no-op stub for the final exec
    anvil.chmod(0o755)
    (tmp_path / ".claude-plugin").mkdir(exist_ok=True)
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{}\n")

    stubbin = tmp_path / "stubbin"
    stubbin.mkdir(exist_ok=True)
    uv = stubbin / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "ANVIL_BIN_DIR": str(tmp_path / "pathdir"),
        "PATH": f"{stubbin}:{os.environ.get('PATH', '')}",
    }
    return subprocess.run(
        ["sh", str(_script()), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )


def test_install_script_is_valid_sh() -> None:
    r = subprocess.run(
        ["sh", "-n", str(_script())], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr


def test_install_script_requires_a_harness_arg() -> None:
    # No arg → usage + exit 2, before any uv/git/network work.
    r = subprocess.run(
        ["sh", str(_script())], capture_output=True, text=True
    )
    assert r.returncode == 2
    assert "Usage" in r.stderr


def test_help_flag_exits_zero_on_stdout() -> None:
    # An explicit -h/--help is not an error: exit 0, and usage goes to stdout
    # (a usage *error* exits 2 to stderr).
    r = subprocess.run(
        ["sh", str(_script()), "--help"], capture_output=True, text=True
    )
    assert r.returncode == 0
    assert "Usage" in r.stdout


def test_install_script_force_updates_cache_and_fails_loud() -> None:
    """A stale cached checkout must be FORCED to latest main, not fail-soft to old
    code: `pull --ff-only || warn` once kept running pre-fix anvil that corrupted
    configs. The updater now resets hard to a fetched ref and exits on failure."""
    text = _script().read_text()
    assert "pull --ff-only" not in text  # no more fail-soft update
    assert "reset --hard" in text and "FETCH_HEAD" in text  # force to latest main
    # Anchor on the unique cache-update error marker, then assert it EXITS nearby
    # (rather than continuing to run stale code). Robust to other elif/else blocks.
    marker = "couldn't update the cached anvil"
    assert marker in text
    assert "exit 1" in text.split(marker, 1)[1][:300]


def test_path_flag_links_anvil_into_path_dir(tmp_path: Path) -> None:
    r = _run_in_fake_checkout(tmp_path, "codex", "--path")
    assert r.returncode == 0, r.stderr
    link = tmp_path / "pathdir" / "anvil"
    assert link.is_symlink()
    assert os.readlink(link) == str(tmp_path / "bin" / "anvil")
    assert "Linked anvil ->" in r.stderr


def test_no_path_flag_does_not_link(tmp_path: Path) -> None:
    r = _run_in_fake_checkout(tmp_path, "codex")
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "pathdir" / "anvil").exists()


def test_path_flag_never_clobbers_an_existing_anvil(tmp_path: Path) -> None:
    # A real `anvil` the user already placed on PATH must survive untouched.
    pathdir = tmp_path / "pathdir"
    pathdir.mkdir()
    existing = pathdir / "anvil"
    existing.write_text("the user's own anvil\n")
    r = _run_in_fake_checkout(tmp_path, "codex", "--path")
    assert r.returncode == 0, r.stderr
    assert not existing.is_symlink()  # left as-is, not replaced by a symlink
    assert existing.read_text() == "the user's own anvil\n"
    assert "already exists" in r.stderr


def test_path_flag_is_idempotent(tmp_path: Path) -> None:
    assert _run_in_fake_checkout(tmp_path, "codex", "--path").returncode == 0
    r2 = _run_in_fake_checkout(tmp_path, "codex", "--path")
    assert r2.returncode == 0, r2.stderr
    link = tmp_path / "pathdir" / "anvil"
    assert link.is_symlink()
    assert "already linked" in r2.stderr
