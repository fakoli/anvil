# Using Anvil on any coding harness

Anvil's engine does not depend on Claude Code. Any harness can drive the full
loop through one of two surfaces:

1. **MCP** — register the `anvil` stdio server, get all 24 tools.
2. **CLI** — `anvil <command>` with `--json` for machine-readable output.

`anvil install <harness>` wires this up for you, in two tiers:

- **Supported end-to-end** — `claude-code`, `codex`, and `openclaw`. `codex` and
  `openclaw` install natively via their own CLI (skills, commands, and — for codex —
  anvil's `AGENTS.md` spliced into a marked, removable block). `claude-code` is the
  anvil **plugin** itself: install it from the marketplace (see below) or wire
  `.mcp.json` by hand — there is no `anvil install claude-code`.
- **MCP-only best-effort** — every other harness: install merges the anvil MCP server
  into the harness's config **where it can write in place**; for the rest (gemini,
  cline, openhands, continue, goose) `anvil mcp-config <harness>` prints the block to
  paste. No instruction splice, no skills drop — point the agent at the repo's
  `AGENTS.md` for usage guidance.

Why tiers? Splicing instruction files and dropping skills into a dozen harnesses was
the blast-radius behind a config-corruption incident. The three supported harnesses
have a stable native surface; everywhere else the MCP server alone delivers the full
toolset with zero file-format risk.

## One command

```bash
anvil install <harness>          # dry-run: prints exactly what it would write
anvil install <harness> --write  # do it (idempotent MCP merge; +AGENTS.md on codex)
```

Flags: `--root <dir>` pins `ANVIL_ROOT` in the written config; `--uv-run` emits
the explicit `uv run` invocation instead of the bash wrapper (Windows / no bash).

### One-liner (no checkout yet)

```bash
curl -fsSL https://raw.githubusercontent.com/fakoli/anvil/main/scripts/install.sh | sh -s -- <harness>
```

Provisions an anvil checkout (cached at `~/.anvil-src`, or `$ANVIL_SRC`) and runs
`anvil install <harness> --write`. Needs `uv` on PATH.

## Harness support

| Harness | Tier | What `anvil install --write` does |
|---|---|---|
| `claude-code` | **supported** | the anvil **plugin** — install from the marketplace (MCP + skills + hooks), or add anvil to a project `.mcp.json` by hand (not an `anvil install` target — see below) |
| `codex` | **supported** | native `codex plugin marketplace add` + `codex mcp add` (skills via plugin) **and** splice `AGENTS.md` |
| `openclaw` | **supported** | native `openclaw mcp add` + `openclaw plugins install` (plugin ships skills + instructions) |
| `cursor` | MCP-only | merge MCP → `~/.cursor/mcp.json` |
| `vscode` / `copilot` | MCP-only | merge MCP → `.vscode/mcp.json` |
| `windsurf` | MCP-only | merge MCP → `~/.codeium/windsurf/mcp_config.json` |
| `zed` | MCP-only | merge MCP → `~/.config/zed/settings.json` (`context_servers`) |
| `opencode` | MCP-only | merge MCP → `opencode.json` (`mcp`, argv-array command) |
| `roo` | MCP-only | merge MCP → `.roo/mcp.json` |
| `amp` | MCP-only | merge MCP → `~/.config/amp/settings.json` (`amp.mcpServers`) |
| `gemini` | MCP-only | MCP ships in `gemini-extension.json` (see `packaging/gemini/`) |
| `cline` | MCP-only | editor-managed settings — `anvil mcp-config cline` prints the block |
| `openhands` | MCP-only | `[mcp].stdio_servers` in `config.toml` — `anvil mcp-config openhands` |
| `continue` | MCP-only | `.continue/mcpServers/anvil.yaml` — `anvil mcp-config continue` |
| `goose` | MCP-only | `extensions` in `~/.config/goose/config.yaml` — `anvil mcp-config goose` |

**MCP-only** harnesses get just the anvil MCP server — no `AGENTS.md` splice, no
skills drop. For those without an in-place writer (gemini, cline, openhands,
continue, goose), run `anvil mcp-config <harness>` to print the paste-ready block —
it tells you which file to paste it into — and see the committed reference under
`packaging/<harness>/`. Aider has no MCP client, so it's intentionally absent.

## Or just use the CLI

```bash
anvil init && anvil prd parse && anvil plan && anvil next
anvil claim T001 && anvil packet T001
anvil submit T001 --evidence … && anvil apply T001
```

Every read command takes `--json`. `AGENTS.md` carries the full MCP-tool ⇄
CLI-command table — codex gets it spliced in automatically; for any MCP-only
harness, point the agent at the repo's `AGENTS.md` (or paste it where the harness
reads instructions) to give it the same map.

## Claude Code

Two options. Install as a plugin from the marketplace (MCP auto-starts, hooks
included):

```
/plugin marketplace add fakoli/anvil
/plugin install anvil@anvil
```

…or skip the plugin and wire anvil as a plain MCP server by adding its block to a
project `.mcp.json` (Claude Code reads it natively) — there is no `anvil install
claude-code` target. The SessionStart/PreToolUse/PostToolUse hooks are
Claude-Code-only conveniences; every state operation stays reachable through the CLI
or MCP server on any harness. See `docs/hooks-reference.md`.

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

## OpenClaw

OpenClaw is its own agent platform with a full CLI — not a Claude `.mcp.json`
bundle. Anvil installs **natively** and touches **none** of your files:

```
openclaw mcp add anvil --no-probe --command bash --arg <…>/bin/anvil-mcp   # register the server
openclaw plugins install anvil --marketplace fakoli/anvil --force          # skills + commands
```

We pass `--no-probe` so a cold-start `uv sync` (which can exceed OpenClaw's 30s
connect probe) can't leave the server unsaved while the plugin installs — OpenClaw
validates it on first use, or run `openclaw mcp doctor` to check. `--force` makes a
re-install refresh the plugin rather than silently keep a stale copy. Undo with
`anvil install openclaw --rollback` (`openclaw mcp unset` + `openclaw plugins
uninstall`). If the `openclaw` CLI isn't on PATH, the commands are printed to run.

**Sandbox prerequisite.** If you enable OpenClaw sandboxing, add anvil's MCP tools to
`sandbox.tools.allow` — otherwise the 24 anvil tools silently vanish in sandboxed
turns. `anvil install openclaw` prints this reminder.

### Gateway cron recipes (opt-in)

OpenClaw's Gateway runs `cron` jobs with **zero active agents at no model cost** — a
natural fit for anvil's lazy leases and finish gate. anvil **never registers** any
cron (the no-files contract); run `anvil install openclaw --cron-recipes` to **print**
ready-to-paste recipes:

- **Queue probe** (every 10m): `anvil next -q` — exits 3 on an empty queue, so a
  command-cron stays quiet until there's ready work. No model cost.
- **Nightly reconcile**: `anvil sync … ; anvil sync --fix --yes ; anvil drift --json`
  (`;` so a failed step — e.g. no GitHub token — doesn't skip the rest).
- **Lease watchdog** (every 15m): `anvil doctor --json || anvil sync --fix --yes` —
  surfaces/repairs stale claims with zero active agents.
- **Finish-gate nudge** (every 30m): `openclaw cron add … --announce <channel>
  --command 'anvil notify-digest'` — `notify-digest` prints a one-line `needs_review`
  + blockers summary, and **nothing** on a clean queue, so the cron's `--announce`
  stays silent until something needs attention. (`--announce` is OpenClaw's flag, not
  anvil's.)

`anvil notify-digest` works on any harness (it's a plain CLI read); `--json` emits the
counts. It's the small net-new verb the channel/finish-gate recipes build on.
