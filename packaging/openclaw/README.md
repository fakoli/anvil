# Anvil on OpenClaw — manifestless Claude bundle

OpenClaw detects Anvil's existing layout directly: no new manifest is required.
Anvil already ships the three things an OpenClaw / Claude bundle is detected by:

- `skills/*/SKILL.md` — the skill set.
- `agents/` — the agent definitions.
- `.mcp.json` — the MCP server block (the `anvil` 24-tool FastMCP stdio server).

## Install

From the Anvil checkout root:

```bash
openclaw plugins install ./
```

## Notes

- **Hooks are detected but not executed.** Anvil's `hooks/` are
  SessionStart/PreToolUse/PostToolUse hooks that are **Claude-Code-only**; an
  OpenClaw install will surface them but will not run them. Every capability is
  still reachable via the CLI/MCP rows in `AGENTS.md` without hooks — this is
  consistent with the "Notes" section of `AGENTS.md`.
- The instruction file is `AGENTS.md` at the checkout root (read natively). Run
  `anvil install openclaw --write` to (re)drop it and merge the `.mcp.json`
  server block into the project root.
