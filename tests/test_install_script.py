"""Guard the one-line installer ``scripts/install.sh``.

Cheap, network-free checks: the script is valid POSIX sh, its advertised harness
list matches the engine registry, ``claude-code`` redirects to the plugin flow, a
missing harness arg exits 2, and — with stubbed ``uv``/``anvil`` on PATH — it
installs the published package via ``uv tool`` and then wires the target harness.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from anvil.cli.install import HARNESSES


def _script() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "install.sh"


def test_usage_harness_list_matches_the_registry() -> None:
    """The hand-typed harness list in install.sh's usage must match the engine's
    registry — exactly, both directions. install.py derives ``--help`` and its
    "unknown harness" error from HARNESSES, so the shell script is the ONE copy
    that can drift; ``claude-code`` is allowed (the script special-cases it to the
    ``/plugin marketplace`` path instead of calling ``anvil install``)."""
    block = _script().read_text().split("harness:", 1)[1].split("}", 1)[0]
    advertised = set(re.findall(r"[a-z][a-z-]+", block)) - {"echo"}
    valid = set(HARNESSES) | {"claude-code"}
    assert advertised == valid, (
        "install.sh usage out of sync with the harness registry — "
        f"only in script: {sorted(advertised - valid)}; "
        f"missing from script: {sorted(valid - advertised)}"
    )


def test_install_script_is_valid_sh() -> None:
    r = subprocess.run(["sh", "-n", str(_script())], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_install_script_requires_a_harness_arg() -> None:
    # No arg → usage + exit 2, before any uv/network work.
    r = subprocess.run(["sh", str(_script())], capture_output=True, text=True)
    assert r.returncode == 2
    assert "Usage" in r.stderr


def test_help_flag_exits_zero_on_stdout() -> None:
    # An explicit -h/--help is not an error: exit 0, usage to stdout.
    r = subprocess.run(["sh", str(_script()), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "Usage" in r.stdout


def test_claude_code_redirects_to_plugin_marketplace() -> None:
    # claude-code is not an `anvil install` target — redirect to the plugin flow
    # (exit 0), not try to install it. Runs before the uv check, so no stubs.
    r = subprocess.run(["sh", str(_script()), "claude-code"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "/plugin marketplace add fakoli/anvil" in r.stdout


def _run_with_stubs(
    tmp_path: Path, *args: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run install.sh with stub ``uv`` + ``anvil`` on PATH that log their argv, so
    we can assert the install path without the network or the real tools."""
    stub = tmp_path / "stubbin"
    stub.mkdir()
    calls = tmp_path / "calls.log"
    uv = stub / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f'printf "uv %s\\n" "$*" >> "{calls}"\n'
        # `uv tool dir --bin` is only consulted on the not-on-PATH branch.
        f'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then echo "{stub}"; fi\n'
        "exit 0\n"
    )
    uv.chmod(0o755)
    anvil = stub / "anvil"
    anvil.write_text("#!/bin/sh\n" f'printf "anvil %s\\n" "$*" >> "{calls}"\nexit 0\n')
    anvil.chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}:{os.environ.get('PATH', '')}", "HOME": str(tmp_path)}
    r = subprocess.run(["sh", str(_script()), *args], capture_output=True, text=True, env=env)
    return r, (calls.read_text() if calls.exists() else "")


def test_script_installs_from_pypi_then_wires_harness(tmp_path: Path) -> None:
    r, logged = _run_with_stubs(tmp_path, "codex")
    assert r.returncode == 0, r.stderr
    # Installs the published package via uv tool (idempotent / upgrading re-run)...
    assert "tool install" in logged and "anvil-state" in logged
    # ...then wires the harness through the installed `anvil`.
    assert "install codex --write" in logged
