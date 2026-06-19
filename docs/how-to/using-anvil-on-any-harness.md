# Using Anvil on any coding harness

Anvil's engine does not depend on Claude Code. Any harness can drive the full
loop through one of two surfaces:

1. **MCP** — register the `anvil` stdio server, get all 24 tools.
2. **CLI** — `anvil <command>` with `--json` for machine-readable output.

You don't have to wire either by hand: `anvil install <harness>` writes the MCP
config **and** drops `AGENTS.md` where the harness reads it.

## One command

```bash
anvil install <harness>          # dry-run: prints exactly what it would write
anvil install <harness> --write  # do it (idempotent merge + AGENTS.md)
```

Flags: `--root <dir>` pins `ANVIL_ROOT` in the written config; `--uv-run` emits
the explicit `uv run` invocation instead of the bash wrapper (Windows / no bash).

### One-liner (no checkout yet)

```bash
curl -fsSL https://raw.githubusercontent.com/fakoli/anvil/main/scripts/install.sh | sh -s -- <harness>
```

Provisions an anvil checkout (cached at `~/.anvil-src`, or `$ANVIL_SRC`) and runs
`anvil install <harness> --write`. Needs `uv` on PATH.

## Supported harnesses

| Harness | MCP config written | Instruction file | `--write` |
|---|---|---|---|
| `claude-code` | `.mcp.json` (or install the plugin — see below) | `CLAUDE.md`/`AGENTS.md` | ✅ |
| `cursor` | `~/.cursor/mcp.json` | `AGENTS.md` | ✅ |
| `codex` | `~/.codex/config.toml` (`[mcp_servers.anvil]`) | `AGENTS.md` | ✅ |
| `vscode` / `copilot` | `.vscode/mcp.json` | `.github/copilot-instructions.md` | ✅ |
| `windsurf` | `~/.codeium/windsurf/mcp_config.json` | `AGENTS.md` | ✅ |
| `zed` | `~/.config/zed/settings.json` (`context_servers`) | `AGENTS.md` | ✅ |
| `opencode` | `opencode.json` (`mcp`, argv-array command) | `AGENTS.md` | ✅ |
| `roo` | `.roo/mcp.json` | `AGENTS.md` | ✅ |
| `amp` | `~/.config/amp/settings.json` (`amp.mcpServers`) | `AGENTS.md` | ✅ |
| `gemini` | ships in `gemini-extension.json` | `AGENTS.md` (contextFile) | instruction only |
| `cline` | editor-managed settings | `AGENTS.md` | instruction only |
| `openhands` | `[mcp].stdio_servers` in `config.toml` (merge by hand) | `AGENTS.md` | instruction only |
| `continue` | `.continue/mcpServers/anvil.yaml` (YAML) | `AGENTS.md` | print + reference |
| `goose` | `extensions` in `~/.config/goose/config.yaml` (YAML) | `AGENTS.md` | print + reference |

For harnesses without an in-place writer (gemini, cline, openhands, continue,
goose), run `anvil mcp-config <harness>` to print the paste-ready block — it also
tells you which file to paste it into — and see the committed reference under
`packaging/<harness>/`. Aider has no MCP client, so it's intentionally absent.

## Or just use the CLI

```bash
anvil init && anvil prd parse && anvil plan && anvil next
anvil claim T001 && anvil packet T001
anvil submit T001 --evidence … && anvil apply T001
```

Every read command takes `--json`. `AGENTS.md` carries the full MCP-tool ⇄
CLI-command table, which Codex/Copilot/Cursor/Windsurf/Cline/Gemini and the rest
read natively.

## Claude Code

Two options. Install as a plugin from the marketplace (MCP auto-starts, hooks
included):

```
/plugin marketplace add fakoli/anvil
/plugin install anvil@anvil
```

…or treat it like any other MCP host with `anvil install claude-code`. The
SessionStart/PreToolUse/PostToolUse hooks are Claude-Code-only conveniences;
every state operation stays reachable through the CLI or MCP server on any
harness. See `docs/hooks-reference.md`.
