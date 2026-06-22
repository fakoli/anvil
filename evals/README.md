# anvil behavioral evals (Layer 3, opt-in, costed)

A small **behavioral-regression** harness that drives a *real* Claude Code agent
through one anvil skill end-to-end against a throwaway anvil project, then asserts
anvil's **own state** (`anvil status`, the workspace `prd.md`, `events.jsonl`)
matches the skill's promise.

This is the **complement** to the deterministic static contract evals:

| Layer | Lives in | Checks | Cost | In fast CI? |
|---|---|---|---|---|
| Static contract | `tests/test_layout_assumptions.py`, `tests/test_skill_cli_contract.py` | Skills cite *real* `anvil` commands/flags; never hard-code an in-repo `.anvil/` path | free, ms | yes |
| **Behavioral (this)** | `evals/` | A real agent, handed the skill, actually drives anvil to the promised *state* | real subscription capacity, ~minutes | **no — opt-in** |

The static evals can prove a skill *says* the right commands. They cannot prove a
real agent *follows* them to the right outcome. Behavioral evals catch semantic /
instruction drift the static checks structurally cannot (e.g. wording that leads
the agent to write the PRD to the wrong place, or skip the parse).

## What the prototype covers

One happy-path flow: the **`start-prd`** skill.

Promise under test: given a rough idea and the six interview answers, `start-prd`
authors a parseable PRD into the workspace and parses it, advancing
`prd-status: none -> draft`.

Assertions (all over anvil's own state, in `cases/start_prd.yaml`):
- `anvil status --json` -> `data.prd_status == "draft"`
- the workspace `prd.md` exists and contains the parser-required sections
- `state.db` and `events.jsonl` exist
- `events.jsonl` carries a `prd.parsed` event

## How it works

1. **Isolate** — `mkdtemp` a scratch project; run every `anvil` command with
   `ANVIL_ROOT=<scratch>`. `ANVIL_ROOT` always wins and is literal
   (`<ANVIL_ROOT>/.anvil`, see `bin/src/anvil/cli/_helpers.py`), so all state
   lands under the scratch dir, a single `rmtree` cleans it up, and the real
   `~/.anvil` is never touched. (`IsolatedEnv`, a context manager.)
2. **Drive** — `claude-agent-sdk` runs a real agent (over its bundled Claude Code
   CLI) with the skill body + the six interview answers inlined into the prompt,
   so it runs unattended. The skill is also copied into
   `<scratch>/.claude/skills/` (mirrors the agent-eval isolator).
3. **Assert** — deterministic pure-Python checks over the resulting state.

## Running it

It is **double-gated** so it never spends capacity by accident:
- it refuses unless `RUN_BEHAVIORAL_EVALS=1`;
- the pytest wrapper additionally skips if `claude-agent-sdk` is not installed.

`claude-agent-sdk` is an **eval-only dependency** (not in `bin/pyproject.toml`,
because it is CLI-wrapper-only and costed). Install it into a throwaway venv:

```bash
uv venv /tmp/anvil-evals
uv pip install --python /tmp/anvil-evals/bin/python claude-agent-sdk anyio pyyaml
```

Then run the standalone runner (prints a pass/fail report):

```bash
RUN_BEHAVIORAL_EVALS=1 /tmp/anvil-evals/bin/python evals/run.py
# or a specific case:
RUN_BEHAVIORAL_EVALS=1 /tmp/anvil-evals/bin/python evals/run.py evals/cases/start_prd.yaml
```

Or via pytest (point pytest at the file from a venv that has both pytest and the
SDK):

```bash
RUN_BEHAVIORAL_EVALS=1 pytest evals/test_behavioral_eval.py -s
```

### Why it is NOT in the fast CI gate

- It **spends real Claude subscription capacity** (one agent run per case).
- It is **latency-nondeterministic** (real model, real tool loop).

The repo's pytest config pins `testpaths = ["../tests"]` (`bin/pyproject.toml`),
so `cd bin && uv run pytest` **never collects `evals/`**. The static evals stay
the fast gate; this is the deliberate, opt-in behavioral gate.

## Auth (important)

The SDK drives the underlying CLI, which resolves auth like interactive Claude
Code:
- if `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY` are set, the CLI uses that **API key**;
- if they are unset, it falls back to the **logged-in subscription session**.

The harness **scrubs `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY` from `os.environ`**
for the duration of the run (and restores them after), so it uses the
subscription session. This is mandatory: the SDK transport builds the subprocess
env as `{**os.environ, **options.env}`, where `options.env` can only *add* keys,
never *remove* an inherited one — so scrubbing the real `os.environ` is the only
way to keep a quota-capped API key from breaking every run with a
`400 usage-limit` error.

Other hardening baked in: `setting_sources=[]` (so ambient Claude Code
hooks/settings don't leak into the subprocess); tolerate the harmless
`opentelemetry` ImportError; and ignore the SDK's trailing terminal control-frame
error *if* a `ResultMessage` was already delivered (the run actually finished).

## Files

- `harness.py` — `IsolatedEnv` (isolation + cleanup), `run_agent` (SDK driver ->
  `ExecutionTrace`), and the `check_*` / `run_assertion` deterministic assertions.
- `cases/start_prd.yaml` — the case: prompt inputs + allowed tools + assertions.
- `run.py` — standalone runner: load a case, isolate, drive, assert, report.
- `test_behavioral_eval.py` — opt-in pytest wrapper (gated + SDK-guarded).

## Extending

Add a `cases/*.yaml` and run it with `run.py <path>`. The assertion vocabulary
(`status_field`, `file_exists`, `file_contains`, `events_contains_action`) covers
most skill outcomes; add a new `check_*` in `harness.py` and wire it into
`run_assertion` for anything else. Natural next flows: `plan` (assert task counts)
and `claim` (assert claim-lease state).

## Last verified run

`start_prd_happy_path`: **PASS** — 6/6 assertions, agent `is_error=False`, 4
turns, `prd_status` advanced `none -> draft`, `prd.parsed` event present. Scratch
dir cleaned up on teardown.
