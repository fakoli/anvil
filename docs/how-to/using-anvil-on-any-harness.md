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
| `openclaw` | `.mcp.json` (manifestless Claude bundle) | `AGENTS.md` | ✅ |
| `cursor` | `~/.cursor/mcp.json` | `AGENTS.md` | ✅ |
| `codex` | native: `codex plugin marketplace add` + `codex mcp add` (Codex writes its own config) | `AGENTS.md` | ✅ |
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

## Codex

Codex has its own plugin + MCP system, so anvil installs **natively** — it never
hand-edits `~/.codex/config.toml` (Codex owns that file). `anvil install codex
--write` runs, on your behalf:

```
codex plugin marketplace add fakoli/anvil       # skills + commands + Plugins-panel entry
codex mcp add anvil -- bash <…>/bin/anvil-mcp   # the MCP server
```

It also splices anvil's usage doc into the project `AGENTS.md` as a marked,
removable block. Undo everything with `anvil install codex --rollback` (it runs
`codex mcp remove` / `codex plugin marketplace remove` and strips the block). If
the `codex` CLI isn't on PATH, the commands are printed for you to run.

### Codex automations (recurring work)

Add `--automations` to also install anvil's scheduled-automation templates into
`~/.codex/automations/` — Codex's native cron-style agent runs, which give anvil
its longer-running-session story (work the queue, reconcile state on a schedule):

```
anvil install codex --write --automations
```

They are installed **`status = "PAUSED"`** with this project's path filled in —
anvil never auto-activates them. Review and turn them on in the Codex app
(Automations). `--rollback` removes them. Templates live under
`packaging/codex/automations/` (`anvil-work-queue`, `anvil-sync-reconcile`).
