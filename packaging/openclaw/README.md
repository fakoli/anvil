# Anvil on OpenClaw — native install via the `openclaw` CLI

OpenClaw is its own agent platform (not a Claude bundle): it manages MCP servers,
skills, and plugins through the `openclaw` CLI and owns its config at
`~/.openclaw/openclaw.json`. Anvil therefore installs **natively** — it never
hand-edits `.mcp.json` or `AGENTS.md`.

## Install

```bash
anvil install openclaw --write
```

runs, on your behalf:

```bash
# register the MCP server (--no-probe: don't block the save on a cold-start
# `uv sync` that can overrun OpenClaw's 30s connect probe)
openclaw mcp add anvil --no-probe --command anvil-mcp
# pull anvil's skills + commands from its Claude-compatible marketplace
# (--force refreshes the plugin on re-install instead of keeping a stale copy)
openclaw plugins install anvil --marketplace fakoli/anvil --force
```

That is the installed-package form. From a source checkout, POSIX hosts use the
wrapper:

```bash
openclaw mcp add anvil --no-probe --command bash --arg <anvil>/bin/anvil-mcp
```

Windows source checkouts, or any install run with `--uv-run`, use the shell-free
form. Dash-leading uv flags are emitted as `--arg=<value>` so OpenClaw treats
them as MCP server arguments:

```bash
openclaw mcp add anvil --no-probe --command uv --arg=run --arg=--quiet --arg=--project --arg=<anvil>/bin --arg=python --arg=-m --arg=anvil.mcp_server
```

If the `openclaw` CLI isn't on PATH, the commands are printed for you to run.
OpenClaw validates the server on first use; run `openclaw mcp doctor` to check.

## Uninstall

```bash
anvil install openclaw --rollback
```

runs `openclaw mcp unset anvil` + `openclaw plugins uninstall anvil --force`. The
global registration is only removed when no other project still references it.

## Native finish-gate plugin (B42 Phase 2, current in v0.4.0)

Anvil ships one **native OpenClaw plugin** at `packaging/openclaw/plugin/` — a
`before_agent_finalize` hook that **blocks an agent from finalizing a turn while
its claimed anvil task lacks submitted verification evidence**. This is stronger
than anvil's Claude-Code hooks, which are non-blocking by design.

It is **opt-in** and anvil registers nothing — `anvil install openclaw
--finish-gate` PRINTS the recipe:

```bash
# 1. Link + enable the plugin
openclaw plugins install --link <anvil>/packaging/openclaw/plugin
openclaw plugins enable anvil-finish-gate
# 2. REQUIRED — before_agent_finalize only fires for a non-bundled plugin once
#    allowConversationAccess is set:
openclaw config set plugins.entries.anvil-finish-gate.hooks.allowConversationAccess true --strict-json
# 3. Restart the Gateway so it loads the plugin
openclaw gateway restart
```

How it works: on `before_agent_finalize` the plugin shells out to
`anvil gate-check --json --actor agent` (cwd-scoped). If the actor holds an active
claim whose task has missing/incomplete evidence, the hook returns
`action:"revise"` (bounded to 3 attempts per run, keyed on task+runId) carrying
anvil's instruction; otherwise it allows finalization.

The same plugin also registers an **`after_tool_call`** hook that auto-captures
evidence: when the `exec` tool runs a verification command (`pytest`, `ruff
check`, `mypy`, `npm test`, `cargo test`, `bun test`), it forwards the command +
exit code + output to `anvil hook capture-evidence`, which appends it to the
active claim's `.anvil/.evidence-buffer/`. This is the OpenClaw-native equivalent
of anvil's Claude-Code `capture-evidence.sh` PostToolUse hook. It is a pure
observer (fire-and-forget) and never blocks or fails a tool call. Note: OpenClaw's
`exec` result combines stdout+stderr into one stream, so captured output is the
combined log.

A third hook, **`before_prompt_build`**, injects a short cacheable anvil-usage note
into the system prompt for anvil-tracked projects (how to claim/submit + a heads-up
that the finish-gate blocks finalizing un-evidenced work). It's gated on a single
memoized `anvil status` probe per workspace (no per-turn shell-out) and returns
nothing for non-anvil projects. Disable it with:

```bash
openclaw config set plugins.entries.anvil-finish-gate.hooks.allowPromptInjection false --strict-json
```

A fourth hook, **`before_tool_call`**, is a **claim-guard**: when a mutating tool
(`write`/`edit`/`apply_patch`) runs while the actor holds no active anvil claim, it
shells out to `anvil claim-guard` and acts per its configured mode. The default is
**`warn`** — it only logs (never blocks; `before_tool_call` can't show the agent
text, so the nudge rides the `before_prompt_build` guidance above). Escalate when
you want enforcement:

```bash
# hard-block unclaimed FILE edits (CI / strict):
openclaw config set plugins.entries.anvil-finish-gate.config.claimGuardMode block --strict-json
# also flag unclaimed exec/bash — WARN-ONLY even in block mode (off by default):
openclaw config set plugins.entries.anvil-finish-gate.config.guardExec true --strict-json
```

- **Only `write`/`edit`/`apply_patch` can be hard-blocked.** `exec`/`bash` (when
  `guardExec=true`) are **warn-only even in `block` mode** — blocking arbitrary
  commands would also block `anvil next`/`anvil claim`, a claim-acquisition deadlock.
- **`require_approval` is for interactive gateways.** With the plugin's
  `timeoutBehavior:"allow"`, an unanswered approval falls **open** after ~30s (it
  does not hard-block on timeout); but if the gateway has **no approval route**
  configured, the request resolves to a denial — so prefer `warn` or `block` when
  unattended.
- **Actor alignment:** the guard checks claims under the actor `agent` (override via
  the `ANVIL_GATE_ACTOR` env, shared with the finish-gate; mode via
  `ANVIL_CLAIM_GUARD_MODE`). If your harness claims under a different identity,
  `block`/`require_approval` could mis-fire — keep the default `warn` until aligned.
- Editing a file outside your claim's declared scope only **warns** (advisory —
  `expected_files` is not exhaustive; a claim with no declared files is not warned).
  The guard is default-OPEN (no project / no cwd / anvil missing / any error ⇒ the
  tool runs).

- **`anvil` must be on the Gateway's PATH** — the plugin spawns it (e.g.
  `install.sh --path`).
- **DEFAULT-OPEN:** no anvil project / no claim for the actor / `anvil` missing /
  any error ⇒ the agent finalizes normally. The gate never crashes or
  false-blocks a turn.
- **What "evidence" means:** `gate-check` uses anvil's own evidence predicate
  (`review.gates.evidence_complete`) — it asserts the task's `required_evidence`
  was *submitted*, **not** that commands exited 0 (anvil records no exit codes on
  Evidence). anvil's *accept* gate is advisory by default (`strict_evidence`); the
  finish-gate enforces submission regardless, to pre-empt the "declare done
  without evidence" failure mode. A true green-tests gate is a separate, larger
  change. The gate evaluates **every** active claim the actor holds (anvil does
  not cap claims per actor) and blocks if any is unverified.
- **Verify it fires** (the Phase-0 smoke check, already confirmed on OpenClaw
  2026.6.6): `openclaw plugins inspect anvil-finish-gate --runtime --json` should
  list `before_agent_finalize` under `typedHooks` once allowConversationAccess is
  set.

## Risk ceiling & PRD scope (v0.4.0)

Beyond the claim-guard, the plugin exposes operator knobs that shape **which
tasks a runner is offered** — set them the same way:

```bash
# only offer tasks whose blast-radius / review-risk is CONFIRMED and <= N (1..5):
openclaw config set plugins.entries.anvil-finish-gate.config.maxBlast 2 --strict-json
openclaw config set plugins.entries.anvil-finish-gate.config.maxReviewRisk 2 --strict-json
# scope discovery to one PRD partition:
openclaw config set plugins.entries.anvil-finish-gate.config.activePrd v0.4.0 --strict-json
```

The plugin exports these to the `anvil` the agent runs —
`maxBlast`→`$ANVIL_MAX_BLAST`, `maxReviewRisk`→`$ANVIL_MAX_REVIEW_RISK`,
`activePrd`→`$ANVIL_PRD` — so `anvil next` honors them with **no per-command
flag**. An explicit ambient env var wins over the config (operator override).

- **`maxBlast` / `maxReviewRisk` are safe-by-construction.** The ceiling offers a
  task only if its risk dimension is **CONFIRMED** *and* within the ceiling —
  unconfirmed / unscored / over-ceiling tasks are withheld, so a low-capability
  runner is never handed high-risk work. Risk scores are confirmed when a task
  passes the `anvil review tasks` gate; a ceiling set against a project whose
  tasks have not been review-confirmed yields an **empty `anvil next` queue**
  (fails safe, never open).
- **`activePrd` takes effect immediately** — it narrows `anvil next` / `anvil
  list` to that PRD's tasks (coordination still spans all PRDs). Omit on a
  single-PRD project.
- **Range-validated on load:** `maxBlast` / `maxReviewRisk` must be integers 1–5;
  an out-of-range value is rejected when the plugin loads. All three are
  additive and default-absent — omitting them preserves pre-v0.4.0 behavior.

## Weak-agent guidance (v0.4.0)

For OpenClaw runners weaker than frontier models (Opus / GPT-5.5), the plugin can
inject a **more explicit, step-by-step, guardrailed** instruction variant into the
system prompt instead of the concise default nudge:

```bash
openclaw config set plugins.entries.anvil-finish-gate.config.guidanceLevel verbose --strict-json
```

- **`standard`** (default) — the one-paragraph "use `anvil next` / `claim` /
  `submit`, the finish-gate blocks unverified turns" nudge. Capable harnesses need
  no more.
- **`verbose`** — a numbered walk-through of the `next → claim → show → work →
  verify → submit` loop plus hard rules (claim before editing; never end a turn
  with unsubmitted evidence; one task at a time), shipped as **`AGENTS-weak.md`**
  alongside the plugin and injected via `before_prompt_build` (cacheable — it
  rides the provider's prompt cache, so it costs tokens once per session, not per
  turn). Also settable via `$ANVIL_GUIDANCE_LEVEL` (env wins over config).

Only the injected guidance changes — the gates (finish-gate, claim-guard) behave
identically at both levels.

## Notes

- **Anvil writes no files for OpenClaw.** No `.mcp.json`, no `AGENTS.md` splice,
  no `.agents/skills` drop — OpenClaw's plugin ships the skills, and `openclaw mcp
  add` writes the server into OpenClaw's own config.
- **Claude-Code `hooks/` are not executed under OpenClaw.** Anvil's `hooks/`
  (SessionStart/PreToolUse/PostToolUse) are Claude-Code-specific; under OpenClaw
  every capability stays reachable via the CLI/MCP rows in `AGENTS.md`. The one
  exception is the **native finish-gate plugin** above, which runs through
  OpenClaw's own `before_agent_finalize` plugin hook (opt-in).
