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

import argparse
import json
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
    run_codex_agent,
    run_assertion,
)

DEFAULT_CASE = EVALS_DIR / "cases" / "start_prd.yaml"


def _build_prompt(case: dict, skill_body: str) -> str:
    """Compose the agent prompt: skill body + inline interview answers + run-it.

    The skill is normally interactive (one interview question per message). For a
    deterministic, unattended eval we hand the agent the answers up front and tell
    it to execute the whole flow without pausing for a human.
    """
    if "prompt" in case:
        return (
            case["prompt"] + "\n\nUse Anvil's CLI for all state mutations. "
            "The current harness owns implementation. Never invoke a nested model provider. "
            "Never run anvil apply --approve or mark tasks done.\n\n" + skill_body
        )
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


def run_case(
    case_path: Path, *, provider: str = "claude", model: str = "gpt-6-astra",
    reasoning_effort: str = "high", report_path: Path | None = None,
) -> bool:
    if os.environ.get("RUN_BEHAVIORAL_EVALS") != "1":
        raise RuntimeError("Behavioral evaluation requires RUN_BEHAVIORAL_EVALS=1.")
    case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    skill_name = case["skill"]
    skill_src = REPO_ROOT / "skills" / skill_name / "SKILL.md"
    skill_body = skill_src.read_text(encoding="utf-8")

    print(f"== eval case: {case['id']} (skill: {skill_name}) ==")
    print(f"   {case.get('description', '').strip()}\n")

    with IsolatedEnv() as env:
        # 1. throwaway project
        env.init(case["project_name"])
        if case.get("config"):
            config_path = env.state_dir / "config.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config.update(case["config"])
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        if case.get("prd_source"):
            (env.state_dir / "prd.md").write_text(case["prd_source"], encoding="utf-8")
        for command in case.get("setup_commands", []):
            env.run_anvil(*command)
        print(f"   scratch project: {env.project_dir}")

        # 2. make the skill discoverable in the project (mirrors agent-eval)
        skill_dest = env.project_dir / (".agents" if provider == "codex" else ".claude")
        skill_dest = skill_dest / "skills" / skill_name
        skill_dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_src, skill_dest / "SKILL.md")

        before = env.status_json().get("data", {}).get("prd_status")
        print(f"   prd_status before: {before!r}")

        # 3. drive a real agent
        prompt = _build_prompt(case, skill_body)
        print(f"   driving agent ({provider}; subscription session)...")
        if provider == "codex":
            trace = run_codex_agent(
                prompt, cwd=env.project_dir, model=model, reasoning_effort=reasoning_effort,
                timeout=float(case.get("timeout_seconds", 600)),
            )
        elif provider == "claude":
            trace = run_agent(
                prompt, cwd=env.project_dir,
                allowed_tools=list(case.get("allowed_tools", ["Bash", "Read", "Write"])),
                max_turns=int(case.get("max_turns", 20)),
                timeout=float(case.get("timeout_seconds", 600)),
                extra_env={"ANVIL_ROOT": str(env.project_dir)},
            )
        else:
            raise ValueError(f"Unknown subscription eval provider: {provider}")
        print(f"   agent: is_error={trace.is_error} turns={trace.num_turns}")
        print(f"   agent result: {trace.result[:200]!r}")
        if trace.is_error:
            print(
                "\n   AGENT RUN ERRORED. Check subscription login, capacity, "
                "and model availability. See evals/README.md.\n"
            )

        after = env.status_json().get("data", {}).get("prd_status")
        print(f"   prd_status after:  {after!r}\n")

        # 4. assert anvil's own state
        results = [run_assertion(env, spec) for spec in case["assertions"]]
        for r in results:
            mark = "PASS" if r.passed else "FAIL"
            print(f"   [{mark}] {r.name}  ({r.detail})")

        passed = not trace.is_error and all(r.passed for r in results)
        if report_path is not None:
            report_path.write_text(json.dumps({
                "case": case["id"], "provider": provider,
                "model": model if provider == "codex" else "subscription-default",
                "reasoning_effort": reasoning_effort if provider == "codex" else None,
                "passed": passed, "duration_seconds": trace.duration_seconds,
                "usage": trace.usage,
                "assertions": [{"name": r.name, "passed": r.passed} for r in results],
            }, indent=2) + "\n", encoding="utf-8")
        npass = sum(1 for r in results if r.passed)
        print(
            f"\n== {'PASS' if passed else 'FAIL'}: {npass}/{len(results)} "
            f"assertions, agent {'ok' if not trace.is_error else 'errored'} =="
        )
        return passed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", nargs="?", type=Path, default=DEFAULT_CASE)
    parser.add_argument("--provider", choices=("claude", "codex"), default="claude")
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh", "max"),
                        default="high")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv[1:])
    if os.environ.get("RUN_BEHAVIORAL_EVALS") != "1":
        print(
            "Refusing to run: this eval spends real subscription "
            "capacity.\nSet RUN_BEHAVIORAL_EVALS=1 to run it deliberately. "
            "See evals/README.md."
        )
        return 2
    case_path = args.case.resolve()
    if not case_path.exists():
        print(f"case not found: {case_path}")
        return 2
    ok = run_case(case_path, provider=args.provider, model=args.model,
                  reasoning_effort=args.reasoning_effort, report_path=args.report)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
