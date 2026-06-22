"""Guard: shipped skills detect `anvil init` the layout-aware way.

Skills must check initialization with `anvil status` (which resolves the default
HOME workspace OR a local in-repo `.anvil/`), NOT a raw `ls .anvil/state.db` —
that path is wrong under the default workspace layout (state lives in
`~/.anvil/workspaces/…`, not the repo), so it reports "missing" even when the
project IS initialized. That false negative caused an init loop when dogfooding
anvil on Codex (which shares this same `skills/` dir via its plugin manifest).

Regression guard for that bug class.
"""

from __future__ import annotations

from pathlib import Path

# Skills whose Prerequisites gate on the project being initialized.
INIT_GATED = {"state-ops", "prd", "start-prd"}


def _skills_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "skills"


def test_no_skill_detects_init_via_in_repo_state_db() -> None:
    """No skill may use `ls .anvil/state.db` as a COMMAND to detect init (a
    backtick mention in explanatory prose is fine — we match line-leading commands)."""
    offenders = []
    for skill in sorted(_skills_dir().glob("*/SKILL.md")):
        for i, line in enumerate(skill.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("ls .anvil/state.db"):
                offenders.append(f"{skill.relative_to(_skills_dir().parent)}:{i}")
    assert not offenders, (
        "These skills detect init via `ls .anvil/state.db`, which is wrong under the "
        f"default HOME workspace layout — use `anvil status` instead: {offenders}"
    )


def test_init_gated_skills_use_anvil_status() -> None:
    """The init-gating skills must positively use the layout-aware `anvil status`
    check in their Prerequisites."""
    for name in sorted(INIT_GATED):
        text = (_skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")
        assert "anvil status >/dev/null" in text, (
            f"skill {name!r} should gate init on `anvil status`, not a path check"
        )
