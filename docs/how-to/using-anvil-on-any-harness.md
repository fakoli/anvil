# Using Anvil on any coding harness

Anvil's engine does not depend on Claude Code. Any harness can drive the full
loop through one of two surfaces. (New to terms like packet, claim, or lease?
See the [glossary](../glossary.md).)

1. **MCP** — register the `anvil` stdio server. It serves the 24 execution
   tools by default; set `ANVIL_MCP_PLANNING=1` in the server's env to expose
   all 36 (adds the planning tools — `init_project`, `parse_prd`, `assess_prd`, `plan_tasks`,
   `score_tasks`, `review_prd`, `review_tasks`, `apply_review_decision`,
   `find_decisions`, `edit_dependencies`, `describe_surface`, `create_bundle`).
2. **CLI** — `anvil <command>` with `--json` for machine-readable output.

`anvil install <harness>` wires this up for you, in two tiers:

- **Supported end-to-end** — `claude-code`, `codex`, and `openclaw`. `codex` and
  `openclaw` install natively via their own CLI (skills, commands, and — for codex —
  anvil's `AGENTS.md` spliced into a marked, removable block). `claude-code` is the
  anvil **plugin** itself: install it from the marketplace (see below) or wire
  `.mcp.json` by hand — there is no `anvil install claude-code`.
- **MCP-only best-effort** — every other harness: install merges the anvil MCP server
  into the harness's config **where it can write in place**; for the rest (`cline`,
  `continue`, `goose`) `anvil mcp-config <harness>` prints the block to paste.
  `gemini` and `openhands` aren't valid `mcp-config` clients — their MCP config
  instead ships as a committed reference file (`packaging/gemini/gemini-extension.json`,
  `packaging/openhands/config.toml.snippet`). No instruction splice, no skills drop —
  point the agent at the repo's `AGENTS.md` for usage guidance.

Why tiers? Splicing instruction files and dropping skills into a dozen harnesses was
the blast-radius behind a config-corruption incident. The three supported harnesses
have a stable native surface; everywhere else the MCP server alone delivers the full
toolset with zero file-format risk.

## One command

```bash
anvil install <harness>          # dry-run (default): prints what it would write, writes nothing
anvil install <harness> --write  # do it (idempotent MCP merge; +AGENTS.md on codex)
```

The dry-run marks every target with `would write` and ends with an explicit
trailer so it can't be mistaken for a write:

```
# MCP config (would write) → ~/.cursor/mcp.json
{
  "mcpServers": {
    "anvil": {
      "command": "anvil-mcp",
      "args": []
    }
  }
}
# dry-run — nothing was written. Re-run with --write to apply.
```

Flags: `--root <dir>` pins `ANVIL_ROOT` in the written config; `--uv-run` emits
the explicit `uv run` invocation instead of the bash wrapper (automatic for
source checkouts on Windows; useful anywhere without bash).

### Install the CLI (no checkout)

Install the published package — it provides `anvil` and `anvil-mcp` on PATH — then
wire a harness:

```bash
uv tool install anvil-state        # or: pipx install anvil-state
anvil install <harness> --write
```

Or do both in one shot:

```bash
curl -fsSL https://raw.githubusercontent.com/fakoli/anvil/main/scripts/install.sh | sh -s -- <harness>
```

Both need `uv` on PATH and nothing is cloned. `uv`'s tool-bin dir (usually
`~/.local/bin`) must be on your PATH so the harness can launch `anvil-mcp` from the
config this writes.

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
| `openhands` | MCP-only | `[mcp].stdio_servers` in `config.toml` — copy `packaging/openhands/config.toml.snippet` |
| `continue` | MCP-only | `.continue/mcpServers/anvil.yaml` — `anvil mcp-config continue` |
| `goose` | MCP-only | `extensions` in `~/.config/goose/config.yaml` — `anvil mcp-config goose` |

**MCP-only** harnesses get just the anvil MCP server — no `AGENTS.md` splice, no
skills drop. For those without an in-place writer, `cline`, `continue`, and `goose`
run `anvil mcp-config <harness>` to print the paste-ready block — it tells you which
file to paste it into — and see the committed reference under `packaging/<harness>/`.
`gemini` and `openhands` aren't valid `mcp-config` clients, so use their committed
manifests directly instead: `packaging/gemini/gemini-extension.json` and
`packaging/openhands/config.toml.snippet`. Aider has no MCP client, so it's
intentionally absent.

## Or just use the CLI

```bash
anvil init && anvil prd parse && anvil plan && anvil next
anvil claim T001 && anvil packet T001
anvil submit T001 --commands "uv run pytest -x" --files-changed "src/foo.py" && anvil apply T001
```

Every read command takes `--json`. `AGENTS.md` carries the full MCP-tool ⇄
CLI-command table — codex gets it spliced in automatically; for any MCP-only
harness, point the agent at the repo's `AGENTS.md` (or paste it where the harness
reads instructions) to give it the same map.

For a milestone that should land as one reviewed delivery, create and claim an execution
bundle instead of handing each task off independently:

```bash
anvil bundle create B001 T001 T002 --prd release --coordinator lead --actor lead
anvil bundle claim B001 --actor lead --shared-tree
anvil bundle packet B001 --actor lead
```

The coordinator-only, bounded-delegation, recovery, and review flow is in
[Coordinating a milestone bundle](coordinating-a-bundle.md).

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
codex plugin marketplace add fakoli/anvil   # skills + commands + Plugins-panel entry
codex mcp add anvil -- anvil-mcp            # the MCP server
```

(That is the installed-package form — `anvil-mcp` is the console script on your
PATH. From a source checkout Codex always uses the shell-free form:
`uv run --quiet --project <checkout>/bin python -m anvil.mcp_server`. The dry-run
shows the exact command.)

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
openclaw mcp add anvil --no-probe --command anvil-mcp              # register the server
openclaw plugins install anvil --marketplace fakoli/anvil --force  # skills + commands
```

(As with codex, that is the installed-package form; from a source checkout the
`--command`/`--arg` pair points at the checkout's `bin/anvil-mcp` bash wrapper on
POSIX. On Windows source checkouts, or when `--uv-run` is set, the printed
command uses `uv run --quiet --project <checkout>/bin python -m anvil.mcp_server`;
uv flags are emitted as `--arg=<value>` so OpenClaw treats them as server args.)

We pass `--no-probe` so a cold-start `uv sync` (which can exceed OpenClaw's 30s
connect probe) can't leave the server unsaved while the plugin installs — OpenClaw
validates it on first use, or run `openclaw mcp doctor` to check. `--force` makes a
re-install refresh the plugin rather than silently keep a stale copy. Undo with
`anvil install openclaw --rollback` (`openclaw mcp unset` + `openclaw plugins
uninstall`). If the `openclaw` CLI isn't on PATH, the commands are printed to run.

**Sandbox prerequisite.** If you enable OpenClaw sandboxing, add anvil's MCP tools to
`sandbox.tools.allow` — otherwise anvil's MCP tools silently vanish in sandboxed
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
