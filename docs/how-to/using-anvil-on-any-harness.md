# Using Anvil on any coding harness

Anvil's engine does not depend on Claude Code. Any harness can drive the full
loop through one of two supported surfaces:

1. **MCP** (Cursor, Windsurf, Cline, VS Code, Zed, Codex, Claude Desktop, …):
   register the `anvil` server, get all 24 tools.
2. **CLI** (any shell / any harness that can run commands): `anvil <command>`
   with `--json` for machine-readable output.

## 1. Configure the MCP server

```bash
anvil mcp-config cursor      # or: windsurf | cline | vscode | zed | codex | claude-code
```

This prints a client-specific config block with the server pointed at this checkout's
`bin/anvil-mcp` by absolute path (no plugin-root token). The command also tells
you which file to paste it into. Flags:

- `--uv-run` — emit `uv run …` instead of the bash wrapper (Windows / no bash).
- `--root <dir>` — pin `ANVIL_ROOT` in the config; omit to use the client's cwd.
- `--json` — emit `{client, target_file, format, config_text}` for scripting.

Client envelope differences are handled for you: `mcpServers` (Cursor/Windsurf/
Cline/Claude Code), `servers` + `type:stdio` (VS Code), `context_servers`
(Zed), and `[mcp_servers.anvil]` TOML (Codex).

## 2. Or just use the CLI

```bash
anvil init && anvil prd parse && anvil plan && anvil next
anvil claim T001 && anvil packet T001
anvil submit T001 --evidence … && anvil apply T001
```

Every read command takes `--json`. See `AGENTS.md` for the full MCP-tool ⇄
CLI-command table, which Codex/Copilot/Cursor/Windsurf/Cline/Gemini read
natively.

## Claude Code-specific pieces

Claude Code's SessionStart/PreToolUse/PostToolUse hooks are Claude-Code-only.
They automate parts of the workflow, but every state operation remains
reachable through the CLI or MCP server. See `docs/hooks-reference.md`.
