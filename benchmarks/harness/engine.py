"""Engine layer: own the real anvil binary and stand up a project from a TaskSpec list.

The benchmark drives the *actual* anvil CLI (the same console script a user
runs), not a reimplementation. This module locates/builds that binary once and renders
a PRD from an internal TaskSpec list, then runs the real setup pipeline
(init -> parse -> review -> approve -> plan -> score -> review tasks) so tasks land in
`ready` and are claimable.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class TaskSpec:
    """One unit of work. We render these into a PRD the real parser consumes."""

    id: str                       # e.g. "T001"
    title: str
    files: tuple[str, ...]        # target files this task "writes" (the likely_files)
    priority: str = "medium"      # high | medium | low
    deps: tuple[str, ...] = ()    # task ids this depends on
    verification: tuple[str, ...] = ("pytest -q",)  # verification commands
    feature: str = "F001"


@dataclass
class RunResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


# --- binary management ------------------------------------------------------

def _plugin_bin_dir() -> Path:
    # benchmarks/harness/engine.py -> plugins/anvil/bin
    return Path(__file__).resolve().parents[2] / "bin"


def _venv_anvil_candidate(bin_dir: Path, *, os_name: str | None = None) -> Path:
    """Return the native console-script path created by ``uv sync``."""
    platform_name = os.name if os_name is None else os_name
    if platform_name == "nt":
        return bin_dir / ".venv" / "Scripts" / "anvil.exe"
    return bin_dir / ".venv" / "bin" / "anvil"


@lru_cache(maxsize=1)
def anvil_binary() -> str:
    """Return an absolute path to a runnable `anvil` console script.

    Prefers an already-synced venv; otherwise runs `uv sync` once. The resulting
    binary is cwd-independent, so each actor subprocess can run it with its own
    working directory (the project under test).
    """
    bin_dir = _plugin_bin_dir()
    candidate = _venv_anvil_candidate(bin_dir)
    if candidate.exists():
        return str(candidate)
    if shutil.which("uv") is None:
        raise RuntimeError(
            "uv not found and no synced venv at "
            f"{candidate}. Install uv or pre-sync the bin project."
        )
    subprocess.run(["uv", "sync", "--quiet"], cwd=bin_dir, check=True)
    if not candidate.exists():
        raise RuntimeError(f"uv sync did not produce {candidate}")
    return str(candidate)


def run(args: list[str], cwd: Path, actor: str | None = None,
        timeout: float = 60.0) -> RunResult:
    """Invoke the real CLI. `actor` is threaded through as --actor where relevant."""
    cmd = [anvil_binary(), *args]
    if actor is not None and "--actor" not in args:
        cmd += ["--actor", actor]
    # Each scenario is an isolated git repo with its own in-repo .anvil/; force the
    # legacy local layout so state stays in `cwd/.anvil` rather than resolving to the
    # shared ~/.anvil/workspaces/<repo>/ home workspace (the production default).
    env = {**os.environ, "NO_COLOR": "1", "ANVIL_STATE_LAYOUT": "local"}
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return RunResult(code=124, out=exc.stdout or "", err="timeout")
    return RunResult(code=proc.returncode, out=proc.stdout, err=proc.stderr)


# --- PRD rendering ----------------------------------------------------------

def render_prd(name: str, tasks: list[TaskSpec]) -> str:
    lines = [
        f"# Project: {name}",
        "",
        "## Summary",
        "Benchmark fixture project. Tasks are synthetic units of coordination work.",
        "",
        "## Goals",
        "- Exercise multi-actor task coordination.",
        "",
        "## Requirements",
        "- R001: Actors complete every task exactly once.",
        "- R002: No two actors mutate the same file concurrently.",
        "",
        "## Features",
        "",
        "### F001: Core",
        "**Requirements:** R001, R002",
        "",
        "## Tasks",
        "",
    ]
    for t in tasks:
        lines.append(f"### {t.id}: {t.title}")
        lines.append(f"**Feature:** {t.feature}")
        lines.append(f"**Priority:** {t.priority}")
        if t.files:
            lines.append(f"**Likely files:** {', '.join(t.files)}")
        if t.deps:
            lines.append(f"**Dependencies:** {', '.join(t.deps)}")
        lines.append("")
        lines.append("**Acceptance criteria:**")
        lines.append(f"- {t.title} completes and writes its target files.")
        lines.append("")
        lines.append("**Verification:**")
        for cmd in t.verification:
            lines.append(f"- `{cmd}`")
        lines.append("")
    return "\n".join(lines)


# --- project setup ----------------------------------------------------------

@dataclass
class Project:
    root: Path
    tasks: list[TaskSpec]
    lease_minutes: float = 60.0

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"


def setup_project(root: Path, name: str, tasks: list[TaskSpec],
                  lease_minutes: float = 60.0) -> Project:
    """Stand up a ready-to-claim anvil project via the real pipeline.

    Raises RuntimeError with captured output if any setup step fails, so a broken
    fixture is loud rather than silently producing zero tasks.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "workspace").mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=False)

    steps = [
        (["init", "--name", name], None),
    ]
    for args, actor in steps:
        r = run(args, cwd=root, actor=actor)
        if not r.ok:
            raise RuntimeError(f"setup `{' '.join(args)}` failed: {r.err or r.out}")

    # Configure a short lease for crash-recovery scenarios.
    _set_lease_minutes(root, lease_minutes)

    (root / ".anvil" / "prd.md").write_text(
        render_prd(name, tasks), encoding="utf-8"
    )

    pipeline = [
        ["prd", "parse"],
        ["prd", "review"],
        ["prd", "review", "--approve"],
        ["plan", "--no-llm"],
        ["score"],
        ["review", "tasks"],
    ]
    for args in pipeline:
        r = run(args, cwd=root)
        if not r.ok:
            raise RuntimeError(f"setup `{' '.join(args)}` failed: {r.err or r.out}")

    proj = Project(root=root, tasks=tasks, lease_minutes=lease_minutes)
    ready = ready_task_ids(proj)
    if not ready:
        raise RuntimeError(
            "setup produced no `ready` tasks; check PRD rendering / review gates"
        )
    return proj


def _set_lease_minutes(root: Path, minutes: float) -> None:
    """Patch default_lease_minutes in config.yaml (line-level, no yaml dep)."""
    cfg = root / ".anvil" / "config.yaml"
    if not cfg.exists():
        return
    # The engine coerces this via int(str(value)); a fractional string raises and
    # silently falls back to 60. So emit an integer (floor 1 minute = the real lease
    # granularity everywhere in anvil).
    value = max(1, int(round(minutes)))
    out = []
    seen = False
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("default_lease_minutes:"):
            out.append(f"default_lease_minutes: {value}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"default_lease_minutes: {value}")
    cfg.write_text("\n".join(out) + "\n", encoding="utf-8")


def ready_task_ids(proj: Project) -> list[str]:
    """Authoritative ready set, read straight from the canonical SQLite state."""
    return _task_ids_with_status(proj, "ready")


def _task_ids_with_status(proj: Project, status: str) -> list[str]:
    import sqlite3
    db = proj.root / ".anvil" / "state.db"
    if not db.exists():
        return []
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "SELECT id FROM tasks WHERE status = ? ORDER BY id", (status,)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    return [r[0] for r in rows]


def expire_claims_for(proj: Project, task_id: str) -> int:
    """Fast-forward: backdate the active lease on `task_id` so the engine's stale-claim
    reaper (which runs before every command) treats it as expired.

    This simulates elapsed time rather than waiting the real lease out. It is needed
    because (engine finding) the CLI `claim` path does not wire `default_lease_minutes`
    from config, so the lease is always the 60-minute default — too long to wait on.
    The recovery itself (reap -> task back to ready -> reclaim -> complete) still runs
    through the real engine.
    """
    import sqlite3
    from datetime import datetime, timedelta
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    db = proj.root / ".anvil" / "state.db"
    con = sqlite3.connect(str(db))
    try:
        cur = con.execute(
            "UPDATE claims SET lease_expires_at = ? "
            "WHERE task_id = ? AND released_at IS NULL",
            (past, task_id),
        )
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def task_status(proj: Project) -> dict[str, str]:
    """Map every task id -> current status, from canonical state."""
    import sqlite3
    db = proj.root / ".anvil" / "state.db"
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT id, status FROM tasks").fetchall()
    finally:
        con.close()
    return {r[0]: r[1] for r in rows}
