# anvil-state

Local-first project state engine: turn rough ideas and PRDs into reviewed,
lockable, evidence-backed work packets that humans and AI coding agents can
coordinate on without conflicts.

`anvil-state` installs the `anvil` CLI and the `anvil-mcp` MCP server.

```bash
uv tool install anvil-state      # or: pipx install anvil-state
anvil --help
anvil install <harness>          # wire anvil into Codex, Cursor, VS Code, ...
```

The MCP server (`anvil-mcp`) exposes anvil's tool surface to any MCP-capable
harness. `anvil install <harness>` writes the MCP config and instruction file for
a target harness (idempotent, backed up, reversible with `--rollback`).

Full documentation, the cross-harness install guide, and the Claude Code plugin
live in the repository: https://github.com/fakoli/anvil
