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

## Notes

- **Anvil writes no files for OpenClaw.** No `.mcp.json`, no `AGENTS.md` splice,
  no `.agents/skills` drop — OpenClaw's plugin ships the skills, and `openclaw mcp
  add` writes the server into OpenClaw's own config.
- **Hooks are Claude-Code-only.** Anvil's `hooks/` (SessionStart/PreToolUse/
  PostToolUse) are not executed under OpenClaw; every capability stays reachable
  via the CLI/MCP rows in `AGENTS.md`.
