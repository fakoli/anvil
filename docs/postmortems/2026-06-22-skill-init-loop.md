# Postmortem: skill init-detection loop under the default workspace layout

- **Date:** 2026-06-22
- **Status:** Resolved (fix shipped in PR #72, commit `07ccc8d`)
- **Severity:** High — agents could not start work on an initialized project
- **Authors:** anvil engineering
- **Blameless:** This document examines systems and process, not people. The goal
  is to keep the same class of bug from recurring.

## 1. Summary

Three shipped agent skills (`state-ops`, `prd`, `start-prd`) detected whether a
project was initialized by running `ls .anvil/state.db`, the legacy in-repo state
path. anvil's default layout moved state to a per-project HOME workspace
(`~/.anvil/workspaces/<key>/.anvil/state.db`) two days before, so that file never
exists under the default. The result: after a successful `anvil init`, the skills
reported "MISSING: run anvil init first" and refused to proceed, trapping agents in
an init loop.

## 2. Impact

- **Affected skills:** `skills/state-ops/SKILL.md`, `skills/prd/SKILL.md`,
  `skills/start-prd/SKILL.md` — the three skills whose Prerequisites gate on the
  project being initialized.
- **Affected harnesses:** every harness that loads the shared root `skills/`
  directory. That is the Claude Code plugin and Codex (its plugin manifest,
  `packaging/codex/.codex-plugin/plugin.json`, points `skills` at `./skills/`).
  Any future harness wired to the same `skills/` would have inherited the bug.
- **User-visible symptom:** after a successful `anvil init`, the agent reported
  the project as not initialized and told the user to run `anvil init` again — an
  init loop. The agent refused to read state, list tasks, or run any further
  `anvil` command.
- **Scope of damage:** correctness/usability, not data. State was intact in the
  HOME workspace the whole time; the skills simply could not find it. The blast
  radius was every brand-new default-layout project driven through these skills.
- **Where it surfaced:** dogfooding anvil on Codex, against a freshly published
  `anvil-state 0.1.1` from PyPI installed via `anvil install codex`. Codex loaded
  the buggy `state-ops` skill and hit the loop.

## 3. Timeline

All dates from `git log` on this repo.

| Date (PT) | Commit | Event |
|---|---|---|
| 2026-06-17 | `f36ec8c` | Import of fakoli-state v1.23.8. The skill prerequisites ship with `ls .anvil/state.db` as the init check. At this point the in-repo `.anvil/` IS the only layout, so the check is correct. |
| 2026-06-19 | `fec3c3c` (PR #42) | `feat(state): default to a shared HOME workspace per project`. The default layout becomes `~/.anvil/workspaces/<key>/.anvil/`; in-repo `.anvil/` becomes opt-in via `ANVIL_STATE_LAYOUT=local`. **The skill prose is not updated.** This is the moment `ls .anvil/state.db` becomes a false negative under the default. |
| 2026-06-20 | `cd76238` (PR #53) | Home-workspace follow-ups (B44). The `init` plugin-root guard is noted as "dead in workspace layout" — a sibling symptom of the same migration, also not swept into the skills. |
| 2026-06-21 | `c8effae` | Release v0.1.0; `anvil-state` published to PyPI (later 0.1.1). |
| 2026-06-21 | (dogfooding) | Bug hit while running anvil on Codex against the published package. |
| 2026-06-21 | `07ccc8d` (PR #72) | Fix: all three skills detect init via `anvil status`; regression guard `tests/test_skill_init_detection.py` added. |

The key gap is **2026-06-19 to 2026-06-21**: roughly two days during which the
default layout and the skill prose disagreed, ending with a public release that
shipped the disagreement to Codex users.

## 4. Root cause (5 whys)

1. **Why did the agent loop on init?** The skills checked `ls .anvil/state.db` and
   the file was absent, so they reported the project as uninitialized.
2. **Why was the file absent?** Under the default workspace layout, state lives at
   `~/.anvil/workspaces/<key>/.anvil/state.db`, not in the repo. The in-repo
   `.anvil/state.db` is only created under the opt-in `local` layout.
3. **Why did the skills check an in-repo path?** They were written when in-repo
   `.anvil/` was the only layout (the v1.23.8 import) and were never updated when
   the default changed in PR #42.
4. **Why weren't they updated alongside PR #42?** PR #42 updated the code and one
   doc (`docs/how-to/state-location.md`) but the agent-facing SKILL.md prose was
   not on anyone's mental checklist for "places that assume where state lives."
   The resolver in `bin/src/anvil/cli/_helpers.py` (`_is_local_layout`,
   default `"workspace"`) is the single source of truth in code, but the skills
   re-encode that knowledge as a hard-coded shell path.
5. **Why does the same knowledge live in two places at all?** Agent-facing
   instructions (SKILL.md) duplicate, in English plus shell snippets, facts that
   the CLI already computes (where state lives, whether the project is
   initialized). There is no mechanism that keeps the prose in sync with the code,
   so a code change silently invalidated the prose.

**Deep cause:** SKILL.md files duplicate operational knowledge that belongs to the
code, with no link keeping the two in sync. A layout-aware command (`anvil status`)
already existed and was the right abstraction; the skills should have called it
instead of reimplementing init-detection with a raw path.

## 5. Why it wasn't caught

- **No skill-vs-CLI eval.** CI runs `uv run pytest -q` over the Python engine plus
  one benchmark scenario (`.github/workflows/ci.yml`). Nothing exercises the
  agent-facing SKILL.md instructions against the real CLI under the default layout.
  The buggy line was a shell snippet inside Markdown — invisible to pytest.
- **Tests pin the layout that hides the bug.** The suite runs with an autouse
  fixture that sets `ANVIL_STATE_LAYOUT=local` (introduced in PR #42 so the
  migration needed zero per-test edits). Every test therefore runs in exactly the
  layout where `ls .anvil/state.db` still works. The default-layout path that
  production and Codex use was never on a test's happy path.
- **Manual testing matched the tests.** Local development tends to reuse a project
  that was set up earlier, so the loop did not reproduce until a clean
  default-layout run on Codex.

## 6. Contributing factors

The workspace-layout migration left local-layout assumptions scattered across the
codebase and the agent instructions. The init-loop was the first one to bite, but
it is not isolated:

- **Dead plugin-root guard (confirmed sibling).** `bin/src/anvil/cli/init_status.py`
  carries a comment noting "the guard was dead in workspace layout — it checked the
  resolved HOME base, never a plugin root" (B44). Same migration, same class of
  stale local-layout assumption, found and patched separately.
- **Stale prose in `claim` skill.** `skills/claim/SKILL.md` still states
  "`.anvil/state.db` must exist" in its Prerequisites. It happens to run
  `anvil status` as the actual check, so it does not loop, but the prose is wrong
  under the default and would mislead a reader.
- **Suspect in-repo path references elsewhere in skills.** Several skills still
  point at in-repo paths that do not exist under the default workspace layout, for
  example `.anvil/prd.md` (read/write/`ls`/`cat`), `.anvil/config.yaml`,
  `.anvil/packets/`, and `.anvil/prd.md.bak` across `prd`, `start-prd`, `plan`,
  `resolve-decisions`, and `execute`. These need an audit to confirm which are
  genuinely in-repo artifacts versus stale workspace-relative references.
- **Migration tooling optimized for "zero test edits."** Pinning `local` layout in
  the test fixture was a clean way to keep the suite green, but it also removed the
  pressure that would have forced default-layout coverage. Convenience for the
  migration traded away the signal that would have caught this.

## 7. What went well

- **Dogfooding caught it.** Running anvil on Codex against the published package
  exercised the real default layout in a real harness and surfaced the loop before
  it spread further.
- **The right abstraction already existed.** `anvil status` was already
  layout-aware (exit 1 uninitialized, exit 0 initialized, resolving the HOME
  workspace or a local `.anvil/`), so the fix was to call it, not to build it.
- **The fix shipped with a regression guard.** `tests/test_skill_init_detection.py`
  asserts no skill uses the in-repo `ls .anvil/state.db` command and that the
  init-gated skills positively use `anvil status`, so this exact regression cannot
  silently return.
- **Tight, well-scoped fix.** PR #72 touched only the three skills and the new
  test, and the commit message explicitly flagged the broader audit and eval
  harness as follow-ups rather than scope-creeping them in.

## 8. Action items

Prioritized; P0 first.

| # | Priority | Action | Owner | Status |
|---|---|---|---|---|
| a | P0 | Build an eval harness that runs SKILL.md instructions against the real CLI under the **default** layout, so skill prose drifting from CLI behavior fails a check. Cover the init gate first, then the full happy path. | anvil eng | being built next |
| b | P0 | Audit every skill for stale local-layout assumptions: in-repo `.anvil/...` reads/writes (`state.db`, `prd.md`, `config.yaml`, `packets/`) that do not exist under the default workspace layout. Fix the `claim` skill prose and any others found. | anvil eng | workflow running |
| c | P1 | Add a convention plus lint: skills must not hard-code `.anvil/...` paths for state discovery; they go through a CLI command (`anvil status`, `anvil show`, etc.). Extend `tests/test_skill_init_detection.py` into a general guard. | anvil eng | open |
| d | P1 | Write a migration checklist for future layout/contract changes: enumerate every place that encodes "where state lives" (resolver, CLI, docs, **all SKILL.md files**, packaging manifests) and require a sweep before the default flips. Reduce reliance on the `local`-pinned test fixture by adding default-layout coverage. | anvil eng | open |

## Lessons

- A command that hides a contract (`anvil status` hides where state lives) is safer
  for agents to call than a raw path. Prefer the command; never re-derive the path.
- When a default changes, the agent-facing instructions are part of the contract
  surface, not documentation to update later. They need the same migration
  discipline as code and tests.
- A test fixture that pins the old behavior to avoid edits also pins away the
  coverage you most need after a default change. Add explicit coverage for the new
  default.
