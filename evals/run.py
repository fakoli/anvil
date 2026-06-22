#!/usr/bin/env python3
"""Run an anvil behavioral-eval case end-to-end and print a pass/fail report.

Usage (from a venv that has claude-agent-sdk + anyio + pyyaml):

    RUN_BEHAVIORAL_EVALS=1 python evals/run.py evals/cases/start_prd.yaml

Or just `python evals/run.py` to run the default start_prd case.

What it does, for the named case:
  1. Make a throwaway anvil project (mkdtemp) and `anvil init` it (ANVIL_ROOT-pinned).
  2. Copy the skill's SKILL.md into <scratch>/.claude/skills/<skill>/ (mirrors
     agent-eval's isolator) and inline the skill body into the agent prompt, with
     the six interview answers fed inline so the agent runs non-interactively.
  3. Drive a real Claude Code agent through it via claude-agent-sdk (subscription
     session; API-key vars scrubbed).
  4. Assert anvil's OWN resulting state (anvil status / workspace prd.md /
     events.jsonl) matches the skill's promise.
  5. Print a report and exit 0 (all assertions passed) or 1 (any failed / error).

COSTED: spends real Claude subscription capacity. NOT part of the CI fast path.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
# Make the sibling `harness` module importable however this script is launched.
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

import yaml  # noqa: E402, I001  (deferred: needs sys.path patch above first)

from harness import (  # noqa: E402, I001  (local module; sys.path patched above)
    REPO_ROOT,
    IsolatedEnv,
    run_agent,
    run_assertion,
)

DEFAULT_CASE = EVALS_DIR / "cases" / "start_prd.yaml"


def _build_prompt(case: dict, skill_body: str) -> str:
    """Compose the agent prompt: skill body + inline interview answers + run-it.

    The skill is normally interactive (one interview question per message). For a
    deterministic, unattended eval we hand the agent the answers up front and tell
    it to execute the whole flow without pausing for a human.
    """
    answers = case["interview_answers"]
    answer_block = "\n".join(
        f"  - {key}: {val.strip()}" for key, val in answers.items()
    )
    return f"""You are executing the anvil `{case['skill']}` skill end-to-end, \
non-interactively. Follow the skill instructions below exactly, but DO NOT pause \
to ask the user any interview questions: the six answers are provided inline. \
Author the PRD from these answers, write it to the anvil workspace at the path \
`anvil status` echoes (its `Path:` line), then parse it with `anvil prd parse`. \
Use the `anvil` CLI for all anvil operations. Do not ask for confirmation; just \
complete the flow.

Project name: {case['project_name']}

Interview answers (Q1..Q6):
{answer_block}

--- SKILL: {case['skill']} ---
{skill_body}
--- END SKILL ---

Now: author the PRD into the workspace and run `anvil prd parse`. When done, \
reply with the single line DONE.
"""


def run_case(case_path: Path) -> bool:
    case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    skill_name = case["skill"]
    skill_src = REPO_ROOT / "skills" / skill_name / "SKILL.md"
    skill_body = skill_src.read_text(encoding="utf-8")

    print(f"== eval case: {case['id']} (skill: {skill_name}) ==")
    print(f"   {case.get('description', '').strip()}\n")

    with IsolatedEnv() as env:
        # 1. throwaway project
        env.init(case["project_name"])
        print(f"   scratch project: {env.project_dir}")

        # 2. make the skill discoverable in the project (mirrors agent-eval)
        skill_dest = env.project_dir / ".claude" / "skills" / skill_name
        skill_dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_src, skill_dest / "SKILL.md")

        before = env.status_json().get("data", {}).get("prd_status")
        print(f"   prd_status before: {before!r}")

        # 3. drive a real agent
        prompt = _build_prompt(case, skill_body)
        print("   driving agent (claude-agent-sdk; subscription session)...")
        trace = run_agent(
            prompt,
            cwd=env.project_dir,
            allowed_tools=list(case.get("allowed_tools", ["Bash", "Read", "Write"])),
            max_turns=int(case.get("max_turns", 20)),
            # The skill resolves anvil state via ANVIL_ROOT; the agent's Bash
            # tool inherits this env so its `anvil` calls hit the scratch project.
            extra_env={"ANVIL_ROOT": str(env.project_dir)},
        )
        print(f"   agent: is_error={trace.is_error} turns={trace.num_turns}")
        print(f"   agent result: {trace.result[:200]!r}")
        if trace.is_error:
            print(
                "\n   AGENT RUN ERRORED. If this is a 400 usage-limit error, the "
                "API key was not scrubbed (or your subscription session is "
                "exhausted). See evals/README.md.\n"
            )

        after = env.status_json().get("data", {}).get("prd_status")
        print(f"   prd_status after:  {after!r}\n")

        # 4. assert anvil's own state
        results = [run_assertion(env, spec) for spec in case["assertions"]]
        for r in results:
            mark = "PASS" if r.passed else "FAIL"
            print(f"   [{mark}] {r.name}  ({r.detail})")

        passed = not trace.is_error and all(r.passed for r in results)
        npass = sum(1 for r in results if r.passed)
        print(
            f"\n== {'PASS' if passed else 'FAIL'}: {npass}/{len(results)} "
            f"assertions, agent {'ok' if not trace.is_error else 'errored'} =="
        )
        return passed


def main(argv: list[str]) -> int:
    if not os.environ.get("RUN_BEHAVIORAL_EVALS"):
        print(
            "Refusing to run: this eval spends real Claude subscription "
            "capacity.\nSet RUN_BEHAVIORAL_EVALS=1 to run it deliberately. "
            "See evals/README.md."
        )
        return 2
    case_path = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_CASE
    if not case_path.exists():
        print(f"case not found: {case_path}")
        return 2
    ok = run_case(case_path)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
