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
openclaw mcp add anvil --no-probe --command bash --arg <…>/bin/anvil-mcp
# pull anvil's skills + commands from its Claude-compatible marketplace
# (--force refreshes the plugin on re-install instead of keeping a stale copy)
openclaw plugins install anvil --marketplace fakoli/anvil --force
```

If the `openclaw` CLI isn't on PATH, the commands are printed for you to run.
OpenClaw validates the server on first use; run `openclaw mcp doctor` to check.

## Uninstall

```bash
anvil install openclaw --rollback
```

runs `openclaw mcp unset anvil` + `openclaw plugins uninstall anvil --force`. The
global registration is only removed when no other project still references it.

## Native finish-gate plugin (B42 Phase 2)

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

## Notes

- **Anvil writes no files for OpenClaw.** No `.mcp.json`, no `AGENTS.md` splice,
  no `.agents/skills` drop — OpenClaw's plugin ships the skills, and `openclaw mcp
  add` writes the server into OpenClaw's own config.
- **Claude-Code `hooks/` are not executed under OpenClaw.** Anvil's `hooks/`
  (SessionStart/PreToolUse/PostToolUse) are Claude-Code-specific; under OpenClaw
  every capability stays reachable via the CLI/MCP rows in `AGENTS.md`. The one
  exception is the **native finish-gate plugin** above, which runs through
  OpenClaw's own `before_agent_finalize` plugin hook (opt-in).
