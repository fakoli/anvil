"""Permanent guard against the in-repo `.anvil/` layout assumption.

anvil's DEFAULT state layout is the HOME workspace (`~/.anvil/workspaces/<key>/`);
the in-repo `<cwd>/.anvil/` is opt-in only (`ANVIL_STATE_LAYOUT=local`). Two whole
classes of bug came from agent-facing surfaces (skills + hooks) hard-coding the
in-repo path:

  - Skills told agents to `ls`/`cat`/`cp` a literal `.anvil/...` path that never
    exists under the default layout (init loops, "no PRD" over an approved one).
  - Hooks fast-pathed on `[ ! -d .anvil ]`, so they silently no-op'd under the
    default layout (evidence capture / file-change / heartbeat / claim-check dead).

This module locks both down so the class can't regress. Skills must go through the
layout-aware CLI, never a literal in-repo path; hooks must fire under the workspace
layout (verified behaviorally with a stub CLI).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SKILLS = _REPO / "skills"
_HOOKS = _REPO / "hooks"


def _posix_bash() -> str | None:
    """Resolve a POSIX bash (Git Bash on Windows), never the System32 WSL stub.

    On Windows a bare-name subprocess resolves ``bash`` via ``CreateProcess``,
    which searches ``System32`` before ``PATH`` and finds the WSL launcher —
    which cannot run a hook referenced by a Windows-filesystem path. Prefer Git
    Bash; return None when only the WSL stub exists so the caller can skip.
    """
    if os.name != "nt":
        return shutil.which("bash")
    candidates: list[Path] = []
    git = shutil.which("git")
    if git:  # ...\Git\cmd\git.exe -> ...\Git\{bin,usr\bin}\bash.exe
        root = Path(git).parent.parent
        candidates += [root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"]
    candidates += [
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    ]
    return next((str(c) for c in candidates if c.exists()), None)


_BASH = _posix_bash()
pytestmark = pytest.mark.skipif(
    _BASH is None, reason="no POSIX bash (Git Bash) available for hook tests"
)

# A shell command operating on a RELATIVE in-repo `.anvil/` path. The negative
# lookbehind excludes `~/.anvil`, `$HOME/.anvil`, `/abs/.anvil`, `${X}/.anvil`
# (those are not the in-repo bug) and the `anvil` CLI (no leading dot).
_FS_CMD = re.compile(
    r"(?:^|[|&;]\s*)(?:ls|cat|cp|mv|rm|mkdir|touch|head|tail|stat|test\s+-[fed])\b[^|;&]*(?<![~/\w}])\.anvil/"
)
_REDIRECT = re.compile(r">>?\s*(?<![~/\w}])\.anvil/")


def _code_block_lines(md: str):
    """Yield (lineno, line) for lines inside ``` fenced code blocks."""
    in_block = False
    for i, line in enumerate(md.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            yield i, line


def test_skills_have_no_in_repo_anvil_commands() -> None:
    """No skill may run a filesystem command on a literal in-repo `.anvil/` path —
    state is in the HOME workspace by default. Use a layout-aware CLI command
    (`anvil status`, `anvil prd parse`, etc.) or the path the CLI echoes instead."""
    offenders = []
    for skill in sorted(_SKILLS.glob("*/SKILL.md")):
        md = skill.read_text(encoding="utf-8")
        for lineno, line in _code_block_lines(md):
            if _FS_CMD.search(line) or _REDIRECT.search(line):
                rel = skill.relative_to(_REPO)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Skills run filesystem commands on in-repo `.anvil/` paths, which break "
        "under the default HOME-workspace layout. Route through the layout-aware "
        "CLI instead:\n  " + "\n  ".join(offenders)
    )


# --- Hooks: behavioral — must fire under the default workspace layout ----------

_WORK_HOOKS = {
    "record-file-change.sh": {
        "tool_name": "Edit",
        "tool_input": {"path": "src/x.py"},
        "session_id": "s1",
    },
    "check-claim.sh": {
        "tool_name": "Edit",
        "tool_input": {"path": "src/x.py"},
        "session_id": "s1",
    },
    "capture-evidence.sh": {
        "tool_input": {"command": "pytest -q"},
        "tool_response": {"exit_code": 0, "stdout": "ok", "stderr": ""},
        "session_id": "s1",
    },
    "heartbeat.sh": {},
}


def _stub_cli(plugin_root: Path, logfile: Path) -> None:
    bindir = plugin_root / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    cli = bindir / "anvil"
    cli.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{logfile}"\nexit 0\n')
    cli.chmod(0o755)


def _run_hook(hook: str, payload: dict, *, home: Path, plugin_root: Path, cwd: Path):
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
    }
    return subprocess.run(
        [_BASH, str(_HOOKS / hook)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(cwd),
        env=env,
    )


def _read_log(log: Path) -> str:
    """The stub CLI only creates the log when invoked; missing == not invoked."""
    return log.read_text() if log.exists() else ""


@pytest.mark.parametrize("hook", sorted(_WORK_HOOKS))
def test_hook_fires_under_home_workspace_layout(hook: str, tmp_path: Path) -> None:
    """With state in the HOME workspace (no local .anvil), the hook MUST still
    invoke the layout-aware CLI — not fast-path out — and MUST NOT create a stray
    in-repo .anvil/."""
    home = tmp_path / "home"
    (home / ".anvil" / "workspaces").mkdir(parents=True)  # workspace layout
    plugin_root = tmp_path / "plugin"
    log = tmp_path / "cli.log"
    _stub_cli(plugin_root, log)
    cwd = tmp_path / "project"
    cwd.mkdir()  # NO local .anvil

    r = _run_hook(hook, _WORK_HOOKS[hook], home=home, plugin_root=plugin_root, cwd=cwd)
    assert r.returncode == 0, f"{hook} exited {r.returncode}: {r.stderr}"
    assert "hook" in _read_log(log), (
        f"{hook} did NOT invoke the CLI under the HOME-workspace layout — it "
        "fast-pathed on a missing in-repo .anvil/ (the B44-2 bug)."
    )
    assert not (cwd / ".anvil").exists(), (
        f"{hook} created a stray in-repo .anvil/ under the workspace layout."
    )


@pytest.mark.parametrize("hook", sorted(_WORK_HOOKS))
def test_hook_fast_paths_when_no_anvil_state(hook: str, tmp_path: Path) -> None:
    """With NO anvil state anywhere (no local .anvil, no HOME workspace), the hook
    must fast-path out without shelling to the CLI — preserves the perf budget for
    users who don't use anvil."""
    home = tmp_path / "home"
    home.mkdir()  # NO ~/.anvil/workspaces
    plugin_root = tmp_path / "plugin"
    log = tmp_path / "cli.log"
    _stub_cli(plugin_root, log)
    cwd = tmp_path / "project"
    cwd.mkdir()

    r = _run_hook(hook, _WORK_HOOKS[hook], home=home, plugin_root=plugin_root, cwd=cwd)
    assert r.returncode == 0, f"{hook} exited {r.returncode}: {r.stderr}"
    assert _read_log(log) == "", (
        f"{hook} shelled to the CLI despite no anvil state anywhere — broke the "
        "fast-path."
    )
